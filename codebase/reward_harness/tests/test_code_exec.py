import os
import sys

import pytest

from conftest import definition
from rlar_harness.runtime.code_exec import CodeExecutor, ScoringRouter, probe_environment
from rlar_harness.runtime.policy import validate_component, validate_pack
from rlar_harness.runtime.errors import ForbiddenAPI
from rlar_harness.runtime.runner import SubprocessRunner, make_execute_request
from rlar_harness.schemas import CodeExecutionSpec

SPEC = CodeExecutionSpec(env_id='stdlib-test', timeout_s=3, max_output_bytes=2048, max_calls_per_example=1)

DOUBLE = '''def score(example, context):
    out = context.run_code('import sys\\nprint(int(sys.stdin.read()) * 2)', stdin=example['response'])
    ok = out['status'] == 'ok' and out['stdout'].strip() == '84'
    return {'raw_score': 1.0 if ok else 0.0, 'feedback': out['status'], 'evidence': [{'stderr': out['stderr'][-200:]}]}
'''


def run_code_definition(source):
    d = definition(sources=[source])
    component = d.components[0].model_copy(update={'required_apis': ['run_code']})
    return d.model_copy(update={'components': [component]})


def execute(d, examples, apis=('run_code',), spec=SPEC, records=None):
    runner = SubprocessRunner()
    runner.scoring_service = ScoringRouter(code=CodeExecutor(
        sys.executable, spec, recorder=(lambda *a: records.append(a)) if records is not None else None))
    request = make_execute_request(definition=d, examples=examples, example_ids=[f'e{i}' for i in range(len(examples))],
        permitted_apis=list(apis), runtime_fingerprint='test', wall_timeout_s=30, cpu_timeout_s=5,
        memory_limit_mb=None, max_output_bytes=65536, max_return_bytes=262144, action_id='a1')
    return [pe[0] for pe in runner.execute(request).per_example]


def test_program_outcomes_are_observations_scored_by_the_component():
    records = []
    ok, failing = execute(run_code_definition(DOUBLE), [{'response': '42'}, {'response': 'not a number'}], records=records)
    assert ok.status == 'ok' and ok.raw_value['raw_score'] == 1.0
    assert failing.status == 'ok' and failing.raw_value['raw_score'] == 0.0 and failing.raw_value['feedback'] == 'error'
    assert [r[2]['status'] for r in records] == ['ok', 'error'] and 'ValueError' in records[1][2]['stderr']


def test_undeclared_or_excess_calls_are_forbidden_not_zero():
    (undeclared,) = execute(run_code_definition(DOUBLE), [{'response': '42'}], apis=())
    assert undeclared.status == 'error' and undeclared.error.code == 'forbidden_api'
    twice = DOUBLE.replace("    ok =", "    context.run_code('print(1)')\n    ok =")
    (excess,) = execute(run_code_definition(twice), [{'response': '42'}])
    assert excess.status == 'error' and excess.error.code == 'forbidden_api'


def test_limits_scrubbed_environment_and_fresh_directory(monkeypatch):
    monkeypatch.setenv('RLAR_AIHUB_API_KEY', 'secret-value')
    executor = CodeExecutor(sys.executable, SPEC)
    env = executor.run('import os; print(sorted(os.environ)); print(os.listdir("."))', '', 3)
    assert env['status'] == 'ok' and 'RLAR_AIHUB_API_KEY' not in env['stdout'] and "['main.py']" in env['stdout']
    assert executor.run('while True: pass', '', 1)['status'] == 'timeout'
    flood = executor.run('print("x" * 100000)', '', 3)
    assert flood['status'] == 'output_limit' and flood['stdout_truncated']
    assert executor.run('raise SystemExit(3)', '', 3)['exit_code'] == 3


def test_environment_is_pinned_and_declared():
    assert probe_environment(sys.executable, SPEC) == []
    pinned = SPEC.model_copy(update={'packages': {'pydantic': '0.0.0'}})
    assert 'pins' in probe_environment(sys.executable, pinned)[0]
    assert 'does not exist' in probe_environment('/nonexistent/python', SPEC)[0]
    executor = CodeExecutor(sys.executable, pinned)
    executor.active = ('r', run_code_definition(DOUBLE).components[0])
    assert executor.handle({'kind': 'run_code', 'source': 'print(1)'})['code'] == 'scoring_service_error'


def test_policy_requires_a_declared_environment():
    from conftest import ROOT
    from rlar_harness.schemas import TaskPack
    pack = TaskPack.model_validate_json((ROOT / 'tests/fixtures/math/task_packs/math_v2.json').read_text())
    with pytest.raises(ValueError, match='code_execution'):
        validate_pack(pack.model_copy(update={'permitted_apis': ['run_code']}))
    validate_pack(pack.model_copy(update={'permitted_apis': ['run_code'], 'code_execution': SPEC}))
    assert 'code_execution' not in pack.model_dump(mode='json')
    component = run_code_definition(DOUBLE).components[0]
    validate_component(component)
    with pytest.raises(ForbiddenAPI):
        validate_component(component.model_copy(update={'required_apis': []}))
    with pytest.raises(ForbiddenAPI):
        validate_component(component.model_copy(update={'source': DOUBLE.replace('context.run_code(', 'exec(')}))
