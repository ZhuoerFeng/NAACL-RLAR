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
        def matrix(prefix, count):
            return {f'{prefix}-{n:02}': [r for r in _RESULTS if r['at_id'] == f'{prefix}-{n:02}'] for n in range(1, count + 1)}
        historical = matrix('AT', 36)
        synthesis = matrix('UAT', 28)
        self_contained = matrix('SC', 5)
        superseded = {
            'AT-09': ['UAT-06', 'UAT-09', 'UAT-10'],
            'AT-10': ['UAT-11', 'UAT-16'],
            'AT-11': ['UAT-01', 'UAT-16'],
            'AT-12': ['UAT-16', 'UAT-27'],
            'AT-28': ['UAT-04', 'UAT-26', 'SC-02'],
            'AT-30': ['UAT-20', 'UAT-21', 'UAT-22'],
            'AT-35': ['UAT-21', 'UAT-22', 'UAT-23'],
        }
        current = {**{k:v for k,v in historical.items() if k not in superseded}, **synthesis, **self_contained}
        p.write_text(json.dumps({'schema_version': 'rlar.acceptance.v2', 'exit_status': exitstatus,
            'collected': session.testscollected, 'historical_matrix': historical,
            'synthesis_matrix': synthesis, 'self_contained_matrix': self_contained,
            'superseded_requirements': superseded,
            'all_current_requirements_passed': exitstatus == 0 and all(v and all(r['result'] == 'passed' for r in v) for v in current.values()),
            'scope': 'offline engineering regression; no real-service or production-isolation certification'}, indent=2))

@pytest.fixture
def config(tmp_path):
    c = resolve_config(load_config(ROOT / 'tests/fixtures/math/config.yaml'), ROOT / 'tests/fixtures/math')
    c.budget.wall_deadline_s = 180
    script = tmp_path / 'reward_turns.json'
    script.write_text(json.dumps({'default': [{'content': turn(definition('1.0')).content}, {'content': turn().content}]}))
    c.role_model('reward_synthesizer').scripted_responses = str(script)
    return c

@pytest.fixture
def source(tmp_path):
    p = tmp_path / 'queries.jsonl'
    p.write_text(json.dumps({'query_id': 'q1', 'query': 'Compute 1/2 + 1/3', 'task_profile_id': 'math_v2', 'reference': '5/6'}) + '\n')
    return p


def definition(expression='numeric_score(example, context)', *, mode='checklist', sources=None, api=False):
    import ast
    template = json.loads((ROOT / 'tests/fixtures/math/reward_responses.json').read_text())['default'][0]['actions'][0]['arguments']['definition']
    helper = (ROOT / 'tests/fixtures/math/reward.py').read_text().replace('def score(', 'def numeric_score(')
    helper = helper[:helper.index("    return {'raw_score'")] + '    return value\n'
    if sources is None:
        sources = ['def score(example, context):\n    return ' + expression + '\n']
    components = []
    for i, source in enumerate(sources):
        try:
            tree = ast.parse(source)
            for fn in tree.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == 'score':
                    for node in ast.walk(fn):
                        if isinstance(node, ast.Return) and not isinstance(node.value, ast.Dict):
                            node.value = ast.Dict(keys=[ast.Constant(k) for k in ('raw_score','feedback','evidence')], values=[node.value or ast.Constant(None),ast.Constant('Authored component fixture'),ast.List(elts=[],ctx=ast.Load())])
            source = ast.unparse(ast.fix_missing_locations(tree)) + '\n'
        except SyntaxError:
            pass
        if 'numeric_score(' in source:
            source += helper
        source += '# component fixture ' + str(i) + '\n'
        components.append({'id':'c'+str(i),'kind':'verifiable','capability_ids':['correctness'], 'criterion':'Declared correctness','source':source,'normalization':{'kind':'identity'},'required_apis':[]})
    template.update(mode=mode, components=components, aggregation={'kind':'identity' if mode=='single' else 'mean'})
    return RewardDefinition.model_validate(template)


def turn(d=None, on_pass='submit'):
    return Turn.actions({'id':'test','tool':'test_reward','arguments':{
        'definition':(d or definition()).model_dump(mode='json'),'dev_profile_id':'math_v2','display_limit':2,'on_pass':on_pass}})


def fixture_decision(evidence):
    """Authored numeric simulation only; it does not certify inferred labels."""
    from copy import deepcopy
    from rlar_harness.evaluation.verifier import rule_decision
    view = deepcopy(evidence)
    view['case']['evidence_source'] = 'human_annotated'
    decision = rule_decision(view)
    decision.rationale = 'Authored engineering verifier fixture; no semantic truth certification.'
    return decision


def reward_calls(root):
    from rlar_harness.trace.export import llm_calls
    return [c for c in llm_calls(root) if c.get('actor_role') == 'reward_synthesizer']


def start_episode(driver, item, config):
    from rlar_harness.episode import EpisodeContext, synthesize_tests
    pack=driver.packs.get(item.record.task_profile_id)
    state,budget,dispatcher,llm=driver._episode(item,pack)
    context=EpisodeContext(state,config,dispatcher,llm,budget,driver.journal,driver.blobs,driver.root,driver.deadline)
    synthesize_tests(item.record,context,dispatcher.role_clients['test_case_synthesizer'],dispatcher.snapshot)
    return state,budget,dispatcher,llm


def pin_human_suite(config, tmp_path):
    """Externally authored test labels bound before a rule-baseline run starts."""
    from rlar_harness.schemas import SuiteDraft
    from rlar_harness.evaluation.suite import admission_binding
    pack = json.loads((Path(config.data.task_pack_root) / 'math_v2.json').read_text())
    suite = json.loads((ROOT / 'tests/fixtures/math/suite_draft.json').read_text())
    for case in suite['cases']:
        case.update(evidence_source='human_annotated', evidence_refs=['annotation:' + case['id']])
        for relation in case.get('relations', []):
            relation['evidence_refs'] = case['evidence_refs']
    draft = SuiteDraft.model_validate(suite)
    examples = {e.id:e for e in draft.examples}
    pack['suite_policy']['trusted_evidence'] = {
        'annotation:' + c.id: admission_binding(c,[examples[i] for i in c.example_ids]) for c in draft.cases}
    inputs=tmp_path/'inputs';packs=inputs/'task_packs';packs.mkdir(parents=True)
    (packs/'math_v2.json').write_text(json.dumps(pack))
    (inputs/'test_responses.json').write_text(json.dumps({'default':[{'content':json.dumps(suite)}]}))
    config.data.task_pack_root=str(packs)
    config.validation.dev_suite_root=str(inputs)
    config.role_model('test_case_synthesizer').scripted_responses=str(inputs/'test_responses.json')


from functools import lru_cache


@lru_cache(maxsize=None)
def run_path(parent, task='TestRun'):
    from rlar_harness.evaluation.standalone import new_run_dir
    return new_run_dir(task, parent=parent)


def pytest_configure(config):
    if config.option.basetemp is None:
        from rlar_harness.evaluation.standalone import new_run_dir
        config.option.basetemp = str(new_run_dir('HarnessTests') / 'pytest')


@pytest.fixture(autouse=True)
def worker_outputs_stay_in_test_run(tmp_path, monkeypatch):
    from rlar_harness.runtime.runner import SubprocessRunner
    original = SubprocessRunner.__init__
    def initialize(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.work_root = tmp_path / 'workers'
        self.work_root.mkdir(exist_ok=True)
    monkeypatch.setattr(SubprocessRunner, '__init__', initialize)
