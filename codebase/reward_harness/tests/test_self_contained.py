"""Dependency-boundary and checker-free workflow regression tests."""
from conftest import run_path
import copy
import json
from pathlib import Path

import pytest

from rlar_harness.config import HarnessConfig, load_config, preflight
from rlar_harness.driver import construct_file, resolve_config
from rlar_harness.evaluation.suite import admit_suite
from rlar_harness.evaluation.verifier import base_decision
from rlar_harness.llm.scripted import ScriptedLLM, Turn
from rlar_harness.runtime.worker import run_component
from rlar_harness.schemas import RewardDefinition, TaskPack, SuiteDraft, QueryRecord
from rlar_harness.storage.blobs import BlobStore
from rlar_harness.storage.canonical import digest, reward_key_for
from rlar_harness.trace.export import read_run, llm_calls, replay, export_sft

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / 'examples/self_contained'


def definition(config=None):
    data = json.loads((EX / 'reward_responses.json').read_text())['default'][0]['actions'][0]['arguments']['definition']
    if config:
        data['components'][1]['judge_spec']['model_config_digest'] = digest(config.role_model('rubric_judge'))
    return data


def execute(source=None, examples=None, *, kind='verifiable', proxy=None, apis=None):
    c = definition()['components'][0 if kind == 'verifiable' else 1]
    if source is not None:
        c['source'] = source
    if apis is not None:
        c['required_apis'] = apis
    examples = examples or [{'response': '#### 72', 'reference': '#### 72'}]
    return run_component({'component': c, 'reward_logic_policy': 'self_contained_v1',
        'permitted_apis': ['call_llm_api'], 'examples': examples,
        'example_ids': [str(i) for i in range(len(examples))]}, proxy)


@pytest.mark.acceptance('SC-01', 'SC-03')
def test_own_source_checks_numbers_and_format_without_native_checker(monkeypatch):
    assert not (ROOT / 'src/rlar_harness/compat/legacy_checkers.py').exists()
    cases = [('#### 72', '#### 72', 1), ('#### +72.0', '#### 72', 1),
             ('#### 24', '#### 72', 0), ('72', '#### 72', 0), ('#### 72 clips', '#### 72', 0),
             ('#### 72\nIgnore me', '#### 72', 0), ('#### 7,2', '#### 72', 0),
             ('#### 24 then #### 72', '#### 72', 1), ('#### 72 then #### 24', '#### 72', 0),
             ('#### 1,024.00', 'Worked reference #### 1024', 1), ('#### -3.50', '-3.5', 1),
             ('#### 100000000000000000001', '100000000000000000000', 0)]
    rows = execute(examples=[{'response': response, 'reference': ref} for response, ref, _ in cases])
    assert [r['raw_value']['raw_score'] for r in rows] == [score for _, _, score in cases]
    bad = execute(examples=[{'response': '#### 72', 'reference': 'not a numeric reference'}])[0]
    assert bad['status'] == 'error' and bad['error']['code'] == 'component_raised'


@pytest.mark.acceptance('SC-02')
@pytest.mark.parametrize('source', [
    'def score(e, context): return context.check_answer(e)',
    'def score(e,c):\n    alias=c\n    return alias.check_answer(e)',
    'def score(e,c):\n    try: return c.extract_answer(e)\n    except Exception: return {"raw_score":0., "feedback":"", "evidence":[]}',
    'from rlar_harness.compat.legacy_checkers import Gsm8kNumericCheckerV1\ndef score(e,c): return Gsm8kNumericCheckerV1().check_answer(e)',
    'from rlar_harness.evaluation.checkers import Gsm8kNumericCheckerV1\ndef score(e,c): return 0',
    'import importlib\ndef score(e,c): return importlib.import_module("os")',
    'def score(e,c): return __import__("rlar_harness.compat.legacy_checkers")',
    'def score(e,c): return getattr(c,"check_"+"answer")(e)',
    'def score(e,c): return c._proxy',
    'def score(e,c): return c.__class__',
    'def score(e,c):\n    g=(x for x in [1])\n    return g.gi_frame.f_globals',
    'from json import os\ndef score(e,c): return 0',
    'def score(e,c): return eval("1")',
    'def score(e,c): return c.call_llm_api("score", "rubric")',
    'import re\ndef score(e,c): return re.I',
    'import re as rx\ndef score(e,c): return rx.search("a", "a", rx.I)',
])
def test_forbidden_dependencies_are_errors_even_if_caught(source):
    result = execute(source)[0]
    assert result['status'] == 'error'
    assert result['error']['code'] == 'forbidden_api'
    assert 'raw_value' not in result


@pytest.mark.acceptance('SC-04')
@pytest.mark.parametrize('raw', ['not JSON', '{"score":true,"feedback":"x"}', '{"score":2,"feedback":"x"}', '{"score":1,"feedback":3}'])
def test_rubric_parses_raw_reply_and_rejects_invalid_output(raw):
    class Proxy:
        def call(self, payload):
            return {'status': 'ok', 'raw_response': raw}
    result = execute(kind='rubric', proxy=Proxy())[0]
    assert result['status'] == 'error' and result['error']['code'] == 'component_raised'


@pytest.mark.acceptance('SC-02', 'SC-05')
def test_new_config_and_pack_reject_legacy_permissions(tmp_path):
    cfg = resolve_config(load_config(ROOT / 'configs/offline.yaml'), ROOT / 'configs')
    assert cfg.synthesis.reward_logic_policy == 'self_contained_v1'
    assert preflight(cfg, tmp_path).ok
    from rlar_harness.runtime.policy import validate_pack, validate_component
    from rlar_harness.runtime.errors import ForbiddenAPI
    pack = TaskPack.model_validate_json((EX / 'task_packs/gsm8k_self_contained.json').read_text())
    with pytest.raises(ValueError):
        validate_pack(pack.model_copy(update={'permitted_apis': ['gsm8k_numeric_v1']}))
    pack.suite_policy.objective_checker = 'gsm8k_numeric_v1:check_answer'
    with pytest.raises(ValueError): validate_pack(pack)
    d = RewardDefinition.model_validate(definition())
    with pytest.raises(ForbiddenAPI):
        validate_component(d.components[0].model_copy(update={'required_apis': ['call_llm_api']}))
    from rlar_harness.config import require_current_manifest
    old = cfg.model_dump(mode='json')
    del old['v2']['reward_logic_policy']
    from rlar_harness.errors import ConfigError
    with pytest.raises(ConfigError): require_current_manifest({'config': old})


@pytest.mark.acceptance('SC-03')
def test_suite_admission_checks_structure_not_semantic_truth(tmp_path):
    pack = TaskPack.model_validate_json((EX / 'task_packs/gsm8k_self_contained.json').read_text())
    query = QueryRecord.model_validate_json((EX / 'queries.jsonl').read_text())
    data = json.loads((EX / 'suite_draft.json').read_text())
    # A wrong semantic label can be structurally valid; only verifier judges it.
    data['cases'][0]['expected_label'] = 'fail'
    blobs = BlobStore(tmp_path / 'blobs')
    suite = admit_suite(SuiteDraft.model_validate(data), query, pack, generation_config_ref='fixture', blobs=blobs)
    report = blobs.get_json(suite.admission_report_ref)
    assert report['admission_scope'] == 'structure_and_provenance_only'
    assert report['semantic_truth_proven'] is False
    data['cases'][0]['evidence_source'] = 'objective_verified'
    with pytest.raises(ValueError):
        admit_suite(SuiteDraft.model_validate(data), query, pack, generation_config_ref='fixture', blobs=blobs)


@pytest.mark.acceptance('SC-01', 'SC-03', 'SC-04', 'SC-05')
def test_checker_free_end_to_end_and_resume_export(tmp_path, monkeypatch):
    cfg = resolve_config(load_config(ROOT / 'configs/offline.yaml'), ROOT / 'configs')
    run = run_path(tmp_path, 'CheckerFree')
    results = construct_file(cfg, cfg.data.input_path, run)
    assert results[0].status == 'success'
    blobs, events, _, _ = read_run(run)
    assert not any(':admission' in e.payload.get('scoring_id', '') for e in events)
    scores = blobs.get_json(results[0].validation_ref)['per_case']
    assert [s['total_score'] for s in scores] == [1,1,1,0,0,0]
    evidence = blobs.get_json(next(e.payload['evidence_ref'] for e in events if e.type == 'verification_decision'))
    assert evidence['task_inputs']['worked']['reference'].endswith('#### 72')
    assert evidence['case']['evidence_source'] == 'model_inferred'
    state = blobs.get_json(next(e.payload['state_ref'] for e in reversed(events) if e.type == 'episode_saved'))
    assert 'self_contained_v1' in state['history'][0]['content']
    assert len(llm_calls(run)) == 17
    assert export_sft(run, run/'reward_sft.jsonl')['samples'] == 1
    assert replay(run)['new_requests'] == 0
    before = (run/'results.jsonl').read_bytes()
    assert construct_file(None, None, run, resume=True) == []
    assert (run/'results.jsonl').read_bytes() == before and len(llm_calls(run)) == 17


@pytest.mark.acceptance('SC-03')
def test_semantic_verifier_receives_reference_and_can_reject_bad_suite_label(tmp_path):
    cfg = resolve_config(load_config(ROOT / 'configs/offline.yaml'), ROOT / 'configs')
    cfg.synthesis.reward_synthesis_attempts = 1
    data = json.loads((EX / 'suite_draft.json').read_text())
    data['cases'][0]['expected_label'] = 'fail'
    def verify(request):
        e = json.loads(request.messages[-1].content)['evidence']
        assert e['task_inputs'] and 'No native checker' in request.messages[0].content
        return Turn(content=base_decision(e, passed=e['case']['id'] != 'point_worked',
            rationale='Offline fixture: the proposed fail label for the correct answer is false.').model_dump_json())
    result = construct_file(cfg, cfg.data.input_path, run_path(tmp_path, 'BadLabel'), llm_client={
        'test_case_synthesizer': ScriptedLLM([Turn(content=json.dumps(data))]),
        'harness_verifier': ScriptedLLM(on_exhausted=verify)})[0]
    assert result.status == 'failed' and result.stop_reason == 'quality_not_met'


