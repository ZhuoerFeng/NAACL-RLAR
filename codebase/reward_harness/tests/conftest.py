from __future__ import annotations
import json
from pathlib import Path
import pytest
from rlar_harness.config import load_config
from rlar_harness.driver import resolve_config
from rlar_harness.schemas import RewardDefinition
from rlar_harness.llm.scripted import Turn

ROOT = Path(__file__).resolve().parents[1]

def pytest_addoption(parser):
    parser.addoption('--acceptance-output', default=None)

_RESULTS = []

def pytest_runtest_makereport(item, call):
    if call.when != 'call' and call.excinfo is None:
        return
    outcome = 'passed' if call.excinfo is None else ('skipped' if call.excinfo.errisinstance(pytest.skip.Exception) else 'failed')
    for mark in item.iter_markers('acceptance'):
        for at in mark.args:
            _RESULTS.append({'at_id': at, 'test': item.nodeid, 'phase': call.when, 'result': outcome})

def pytest_sessionfinish(session, exitstatus):
    output = session.config.getoption('--acceptance-output')
    if output:
        p = Path(output); p.parent.mkdir(parents=True, exist_ok=True)
        mapping = {f'AT-{n:02}': [r for r in _RESULTS if r['at_id'] == f'AT-{n:02}'] for n in range(1, 37)}
        p.write_text(json.dumps({'schema_version': 'rlar.acceptance.v1', 'exit_status': exitstatus,
            'collected': session.testscollected, 'matrix': mapping,
            'all_p0_passed': all(v and all(r['result'] == 'passed' for r in v) for v in mapping.values())}, indent=2))

@pytest.fixture
def config():
    c = resolve_config(load_config(ROOT / 'configs/offline_demo.yaml'), ROOT / 'configs')
    c.budget.wall_deadline_s = 180
    return c

@pytest.fixture
def source(tmp_path):
    p = tmp_path / 'queries.jsonl'
    p.write_text(json.dumps({'query_id': 'q1', 'query': 'Compute 1/2 + 1/3', 'task_profile_id': 'math_v1', 'reference': '5/6'}) + '\n')
    return p


def definition(expression='context.check_answer(example)', *, mode='checklist', sources=None, api=True):
    if sources is None:
        sources = ['def score(example, context):\n    return ' + expression + '\n']
    return RewardDefinition.model_validate({'mode': mode, 'components': [
        {'id': 'c' + str(i), 'criterion': 'Declared correctness', 'source': src,
         'normalization': {'kind': 'identity'}, 'required_apis': ['rational_arithmetic_v1'] if api else []}
        for i, src in enumerate(sources)], 'aggregation': {'kind': 'identity' if mode == 'single' else 'mean'},
        'runtime_contract': {'environment_ref': 'python-stdlib-fixture-v1'}})


def turn(d=None, on_pass='submit'):
    return Turn.actions({'id': 'test', 'tool': 'test_reward', 'arguments': {
        'definition': (d or definition()).model_dump(mode='json'), 'dev_profile_id': 'math_v1',
        'display_limit': 2, 'on_pass': on_pass}})
