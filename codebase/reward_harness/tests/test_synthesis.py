from __future__ import annotations

from conftest import run_path
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from rlar_harness.config import load_config, preflight
from rlar_harness.driver import construct_file, resolve_config
from rlar_harness.evaluation.suite import admit_suite, assert_frozen
from rlar_harness.evaluation.verifier import base_decision, check_decision
from conftest import fixture_decision as rule_decision
from rlar_harness.llm.scripted import ScriptedLLM, Turn
from rlar_harness.runtime.aggregate import aggregate
from rlar_harness.schemas import (ComponentResult, QueryRecord, RewardDefinition, SuiteDraft, TaskPack)
from rlar_harness.storage.blobs import BlobStore
from rlar_harness.storage.canonical import digest, reward_key_for
from rlar_harness.trace.export import export_feedback, export_llm_calls, export_sft, llm_calls, read_run, replay
from rlar_harness.trace.report import report

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def synthesis():
    return resolve_config(load_config(ROOT / 'tests/fixtures/math/config.yaml'), ROOT / 'tests/fixtures/math')


@pytest.fixture
def suite_data():
    return json.loads((ROOT / 'tests/fixtures/math/suite_draft.json').read_text())


def admission(tmp_path, suite_data, synthesis, pack=None):
    pack = pack or TaskPack.model_validate_json((ROOT / 'tests/fixtures/math/task_packs/math_v2.json').read_text())
    query = QueryRecord.model_validate_json((ROOT / 'tests/fixtures/math/queries.jsonl').read_text())
    return admit_suite(SuiteDraft.model_validate(suite_data), query, pack,
                       generation_config_ref=digest(synthesis), blobs=BlobStore(tmp_path / 'blobs'))


def reward(synthesis):
    data = json.loads((ROOT / 'tests/fixtures/math/reward_responses.json').read_text())['default'][0]['actions'][0]['arguments']['definition']
    data['components'][1]['judge_spec']['model_config_digest'] = digest(synthesis.role_model('rubric_judge'))
    return data


def reward_turn(data):
    return Turn.actions({'id': 'test', 'tool': 'test_reward', 'arguments': {'definition': data, 'dev_profile_id': 'math_v2'}})


def run(tmp_path, synthesis, adapters=None):
    root = run_path(tmp_path, 'SynthesisCase')
    result = construct_file(synthesis, synthesis.data.input_path, root, llm_client=adapters)[0]
    return root, result


@pytest.mark.acceptance('UAT-01', 'UAT-02', 'UAT-04', 'UAT-05', 'UAT-20', 'UAT-21', 'UAT-22', 'UAT-27')
def test_end_to_end_shared_model_roles_and_exports(tmp_path, synthesis, suite_data):
    # Identical endpoint/model identity is deliberately irrelevant to target filtering.
    synthesis.models['base_rewards'] = synthesis.models['base_tests'].model_copy(deep=True)
    d = reward(synthesis)
    adapters = {'test_case_synthesizer': ScriptedLLM([Turn(content=json.dumps(suite_data))]),
                'reward_synthesizer': ScriptedLLM([reward_turn(d)])}
    root, result = run(tmp_path, synthesis, adapters)
    assert result.status == 'success'
    blobs, events, _, _ = read_run(root)
    validation = blobs.get_json(result.validation_ref)
    assert validation['schema_version'] == 'rlar.validation.v2'
    assert validation['assurance'] == 'behavioral_prototype'
    assert [s['total_score'] for s in validation['per_case']] == [1, 0]
    assert all(c['feedback'] for s in validation['per_case'] for c in s['component_results'])
    assert len(validation['decisions']) == 3
    calls = llm_calls(root)
    assert len(calls) == 7  # 2 synthesizers + 2 unique candidate judge calls + 3 verifier calls
    assert {c['actor_role'] for c in calls} == {'test_case_synthesizer', 'reward_synthesizer', 'rubric_judge', 'harness_verifier'}
    assert calls[0]['model_config_ref'] == next(c for c in calls if c['actor_role'] == 'reward_synthesizer')['model_config_ref']
    frozen_seq = next(e.seq for e in events if e.type == 'suite_frozen')
    assert all(e.seq > frozen_seq for e in events if e.type == 'llm_prepared' and e.payload['actor_role'] == 'reward_synthesizer')
    assert export_llm_calls(root, root / 'calls.jsonl')['physical_attempts'] == 7
    before = (root / 'trace.jsonl').read_bytes()
    assert len(replay(root)['role_episodes']) == 2
    for role in ('test_case_synthesizer', 'reward_synthesizer'):
        for fmt in ('per_call', 'full_trace'):
            out = root / (role + fmt + '.jsonl')
            manifest = export_sft(root, out, actor_role=role, format=fmt)
            assert manifest['samples'] == 1
            assert manifest['tool_model_targets'] == {'harness_verifier': 0, 'rubric_judge': 0}
            row = json.loads(out.read_text())
            assert row['actor_role'] == role
            if fmt == 'full_trace':
                assert sum(row['message_loss_mask']) == 1
    assert export_feedback(root, root / 'feedback.jsonl')['rows'] == 4
    assert (root / 'trace.jsonl').read_bytes() == before
    r = report(root)
    assert r['controller_logical_calls'] == 0 and r['physical_requests'] == 7
    assert r['scoring_requests'] == 2
    assert r['episodes'][0]['outcome'] == 'done'


@pytest.mark.acceptance('UAT-03', 'UAT-11')
@pytest.mark.parametrize('mutation', ['label', 'human', 'missing_category', 'duplicate', 'foreign_reference', 'context_override'])
def test_suite_admission_rejects_invalid_basis(tmp_path, synthesis, suite_data, mutation):
    if mutation == 'label': suite_data['cases'][0]['expected_label'] = 'fail'
    if mutation == 'human': suite_data['cases'][0]['evidence_source'] = 'human_annotated'
    if mutation == 'missing_category': suite_data['cases'].pop(0)
    if mutation == 'duplicate': suite_data['examples'].append(suite_data['examples'][0])
    if mutation == 'foreign_reference': suite_data['cases'][0]['evidence_refs'] = ['invented proof']
    if mutation == 'context_override': suite_data['examples'][0]['allowed_reference_or_metadata'] = {'reference': '1/6'}
    with pytest.raises(ValueError): admission(tmp_path, suite_data, synthesis)


@pytest.mark.acceptance('UAT-02', 'UAT-11')
def test_triplet_equivalence_freezing_and_contradictions(tmp_path, synthesis, suite_data):
    pack = TaskPack.model_validate_json((ROOT / 'tests/fixtures/math/task_packs/math_v2.json').read_text())
    pack.suite_policy.ranking_layout = 'triplet'
    suite_data['examples'].append({**suite_data['examples'][1], 'id': 'bad2', 'response': r'\boxed{2/6}'})
    rank = suite_data['cases'][-1]
    rank['example_ids'].append('bad2')
    rank['relations'] += [{**rank['relations'][0], 'right': 'bad2'},
                         {**rank['relations'][0], 'left': 'bad', 'right': 'bad2', 'operator': '='}]
    frozen = admission(tmp_path, suite_data, synthesis, pack)
    assert len(frozen.examples) == 3 and len(frozen.cases[-1].relations) == 3
    frozen.cases.pop()
    with pytest.raises(ValueError, match='digest'): assert_frozen(frozen)
    rank['relations'].append({**rank['relations'][0], 'left':'bad2', 'right':'good', 'operator':'>'})
    with pytest.raises(ValueError): admission(tmp_path, suite_data, synthesis, pack)
    rank['relations'].pop()
    rank['relations'].pop()
    with pytest.raises(ValueError): admission(tmp_path, suite_data, synthesis, pack)


@pytest.mark.acceptance('UAT-03', 'UAT-11')
def test_inferred_relation_cycles_rejected(tmp_path, synthesis, suite_data):
    from rlar_harness.evaluation.suite import _consistent_relations
    from rlar_harness.schemas import Relation
    relations = [Relation(left=a, right=b, operator=o, justification='basis', evidence_refs=['task'])
        for a, b, o in [('a', 'b', '='), ('b', 'c', '>'), ('c', 'a', '>')]]
    with pytest.raises(ValueError, match='cycle'): _consistent_relations(relations)


@pytest.mark.acceptance('UAT-07', 'UAT-08', 'UAT-15', 'UAT-16', 'UAT-23')
def test_constant_scores_fail_with_finite_revisions_but_suite_still_exports(tmp_path, synthesis):
    synthesis.synthesis.reward_synthesis_attempts = 2
    d = reward(synthesis)
    d['components'] = [d['components'][0]]
    d['components'][0]['source'] = 'def score(example, context):\n    return {"raw_score": 1.0, "feedback": "I found every defect", "evidence": []}'
    root, result = run(tmp_path, synthesis, {'reward_synthesizer': ScriptedLLM([reward_turn(d), reward_turn(d)])})
    assert result.status == 'failed' and result.stop_reason == 'quality_not_met'
    calls = llm_calls(root)
    assert sum(c['actor_role'] == 'reward_synthesizer' for c in calls) == 2
    assert sum(c['actor_role'] == 'harness_verifier' for c in calls) == 3  # unchanged candidate reuses valid false decisions
    assert export_sft(root, root / 'tests.jsonl', actor_role='test_case_synthesizer')['samples'] == 1
    assert export_sft(root, root / 'rewards.jsonl', actor_role='reward_synthesizer')['samples'] == 0


@pytest.mark.acceptance('UAT-14', 'UAT-16')
def test_invalid_verifier_references_cannot_submit(tmp_path, synthesis):
    synthesis.synthesis.verification_format_attempts = 2
    def bad(request):
        e = json.loads(request.messages[-1].content)['evidence']
        result = base_decision(e, passed=True).model_dump(mode='json')
        result['reward_key'] = 'different-version'
        return Turn(content=json.dumps(result))
    root, result = run(tmp_path, synthesis, {'harness_verifier': ScriptedLLM(on_exhausted=bad)})
    assert result.status == 'failed' and result.stop_reason == 'verification_unavailable'
    assert sum(c['actor_role'] == 'harness_verifier' for c in llm_calls(root)) == 6


@pytest.mark.acceptance('UAT-17')
def test_read_only_loop_has_finite_budget(tmp_path, synthesis):
    synthesis.synthesis.max_reward_decisions = 2
    turn = Turn.actions({'id': 'read', 'tool': 'read_resource', 'arguments': {'resource_id': 'frozen_suite'}})
    root, result = run(tmp_path, synthesis, {'reward_synthesizer': ScriptedLLM([turn, turn])})
    assert result.stop_reason == 'budget_exhausted'
    assert sum(c['actor_role'] == 'reward_synthesizer' for c in llm_calls(root)) == 2


@pytest.mark.acceptance('UAT-24', 'UAT-25')
def test_scores_only_no_report_resource_bypass(tmp_path, synthesis):
    synthesis.synthesis.feedback = 'scores_only'
    synthesis.synthesis.backend = 'rule_baseline'
    from conftest import pin_human_suite
    pin_human_suite(synthesis, tmp_path)
    d = reward(synthesis)
    d['components'] = [d['components'][0]]
    d['components'][0]['source'] = 'def score(example, context):\n    return {"raw_score": 1., "feedback": "SECRET_LANGUAGE_MARKER", "evidence": []}'
    def next_turn(request):
        observation = json.loads(request.messages[-1].content)
        if 'results' in observation and observation['results'][0]['tool'] == 'test_reward':
            rid = observation['results'][0]['result']['validation_run_id']
            return Turn.actions({'id': 'read', 'tool': 'read_resource', 'arguments': {'resource_id': 'report:' + rid, 'limit': 16000}})
        assert 'SECRET_LANGUAGE_MARKER' not in request.messages[-1].content
        return reward_turn(reward(synthesis))
    root, result = run(tmp_path, synthesis, {'reward_synthesizer': ScriptedLLM([reward_turn(d)], on_exhausted=next_turn)})
    assert result.status == 'success'
    assert not any(c['actor_role'] == 'harness_verifier' for c in llm_calls(root))
    b, events, _, _ = read_run(root)
    state = b.get_json([e for e in events if e.type == 'episode_saved'][-1].payload['state_ref'])
    assert all('SECRET_LANGUAGE_MARKER' not in m['content'] for m in state['history'] if m['actor'] == 'harness_observation')


@pytest.mark.acceptance('UAT-18')
@pytest.mark.parametrize('point', ['suite_after_freeze', 'llm_after_response', 'verifier_after_decision', 'after_result_before_checkpoint'])
def test_crash_resume_preserves_role_histories_and_budget(tmp_path, synthesis, point):
    root = run_path(tmp_path, 'Resume')
    cfg = tmp_path / 'config.json'
    cfg.write_text(synthesis.model_dump_json())
    command = [sys.executable, '-m', 'rlar_harness', 'construct', '--config', str(cfg), '--run-dir', str(root)]
    p = subprocess.run(command, env={**os.environ, 'RLAR_TEST_KILL_AT': point}, capture_output=True)
    assert p.returncode != 0
    rows = construct_file(None, None, root, resume=True)
    b, events, results, _ = read_run(root)
    assert len(results) == 1 and results[0].status == 'success'
    assert len(llm_calls(root)) == 7
    assert replay(root)['new_requests'] == 0
    suite_refs = {b.get_json(e.payload['state_ref']).get('suite_ref') for e in events if e.type == 'episode_saved'} - {None}
    assert len(suite_refs) == 1




@pytest.mark.acceptance('UAT-26')
def test_utility_single_call_and_physical_budget(tmp_path, synthesis):
    d = reward(synthesis)
    d['components'][1]['source'] = d['components'][1]['source'].replace('return parse_judge(context.call_llm_api',
        'context.call_llm_api(message, context.judge_spec["model_ref"])\n    return parse_judge(context.call_llm_api')
    synthesis.synthesis.reward_synthesis_attempts = 1
    root, result = run(tmp_path, synthesis, {'reward_synthesizer': ScriptedLLM([reward_turn(d)])})
    assert result.status == 'failed'
    assert sum(c['actor_role'] == 'rubric_judge' for c in llm_calls(root)) == 2
    assert report(root)['latest_run_budget']['consumed']['model_requests'] == 4


def test_preflight_lists_all_missing_roles(synthesis):
    synthesis.roles = {}
    out = preflight(synthesis, ROOT)
    assert not out.ok
    assert sum('roles.' in p for p in out.problems) == 4


@pytest.mark.acceptance('UAT-06', 'UAT-09', 'UAT-10', 'UAT-12', 'UAT-13')
def test_capability_evidence_small_total_penalty_partial_and_functional_mapping(tmp_path, synthesis, suite_data):
    from rlar_harness.evaluation.evaluator import TrustedEvaluator
    from rlar_harness.evaluation.verifier import HarnessVerifier, evidence_for
    from rlar_harness.runtime.runner import SubprocessRunner
    pack = TaskPack.model_validate_json((ROOT / 'tests/fixtures/math/task_packs/math_v2.json').read_text())
    frozen = admission(tmp_path, suite_data, synthesis)
    d = reward(synthesis)
    d['capabilities'].append({'id': 'format', 'description': 'Other abilities', 'task_requirement_ref': 'math_v2'})
    d['components'] = [d['components'][0]] + [
        {'id': f'other{i}', 'kind': 'verifiable', 'capability_ids': ['format'], 'criterion': f'Distinct diagnostic {i}',
         'source': f'def score(e,c):\n    # diagnostic {i}\n    return {{"raw_score": 1., "feedback": "okay", "evidence": []}}',
         'normalization': {'kind': 'identity'}} for i in range(9)]
    definition = RewardDefinition.model_validate(d)
    runner = SubprocessRunner()
    runner.work_root = tmp_path
    evaluator = TrustedEvaluator(runner)
    scores, _ = evaluator.score_examples(definition, pack,
        [{'query': 'x', 'reference': '5/6', 'response': e.response} for e in frozen.examples],
        [e.id for e in frozen.examples], action_id='actual-component-execution', reward_key=reward_key_for(definition))
    assert [s.total_score for s in scores] == [1, .9]
    verifier = HarnessVerifier(synthesis.synthesis.model_copy(update={'backend': 'rule_baseline'}))
    e = evidence_for(frozen.cases[1], frozen, definition, scores, pack, verifier, query=QueryRecord(query_id="v2_math", query="x", reference="5/6", task_profile_id="math_v2"))
    e['case']['evidence_source'] = 'human_annotated'
    assert verifier.verify(e, 'case').passed is True
    # An unrelated execution error preserves relevant capability evidence.
    e['scores'][0]['component_results'][-1].update(status='error', score=None)
    assert verifier.verify(e, 'case').passed is True
    # A missing required component cannot be counted as successful defect detection.
    e['scores'][0]['component_results'][0].update(status='error', score=None)
    assert verifier.verify(e, 'case').operation_status == 'insufficient_evidence'
    e = evidence_for(frozen.cases[-1], frozen, definition, scores, pack, verifier, query=QueryRecord(query_id="v2_math", query="x", reference="5/6", task_profile_id="math_v2"))
    e['case']['evidence_source'] = 'human_annotated'
    assert verifier.verify(e, 'ranking').passed
    # Functional mapping may cite a real component even when declared IDs differ.
    e['components'][0]['capability_ids'] = ['format']
    decision = base_decision(e, passed=True, relevant=['exact'])
    check_decision(decision, e)
    decision.relevant_component_ids = ['invented']
    with pytest.raises(ValueError): check_decision(decision, e)


@pytest.mark.acceptance('UAT-19')
def test_finalizer_rejects_changed_verifier_or_suite(tmp_path, synthesis):
    from rlar_harness.driver import RunDriver
    from rlar_harness.inputs import iter_jsonl
    from rlar_harness.episode import EpisodeContext, synthesize_tests
    root, result = run(tmp_path, synthesis)
    with RunDriver(root, resume=True) as driver:
        item = next(iter_jsonl(Path(synthesis.data.input_path)))
        pack = driver.packs.get(item.record.task_profile_id)
        state, budget, dispatcher, client = driver._episode(item, pack)
        context = EpisodeContext(state, synthesis, dispatcher, client, budget, driver.journal, driver.blobs, root, driver.deadline)
        synthesize_tests(item.record, context, dispatcher.role_clients['test_case_synthesizer'], dispatcher.snapshot)
        selected = state['selected']
        dispatcher.finalize(selected['definition_ref'], selected['validation_ref'])
        dispatcher.evaluator.verifier.config_digest = 'changed'
        with pytest.raises(ValueError, match='configuration'): dispatcher.finalize(selected['definition_ref'], selected['validation_ref'])
        for c in dispatcher.role_clients.values(): c.adapter.close()


@pytest.mark.acceptance('UAT-24')
def test_category_ablation_before_freeze_and_independent_audit(tmp_path, synthesis, suite_data):
    from rlar_harness.cli import score_file, audit_run
    pack = TaskPack.model_validate_json((ROOT / 'tests/fixtures/math/task_packs/math_v2.json').read_text())
    pack.suite_policy.categories = ['fail', 'pass']
    with pytest.raises(ValueError, match='removed'): admission(tmp_path, suite_data, synthesis, pack)
    suite_data['cases'].pop()
    assert admission(tmp_path, suite_data, synthesis, pack).suite_digest
    root, result = run(tmp_path, synthesis)
    candidates = tmp_path / 'candidates.jsonl'
    candidates.write_text(json.dumps({'query_id': 'v2_math', 'response': r'\boxed{5/6}', 'candidate_id': 'new'}) + '\n')
    scored = score_file(root, candidates, root / 'scores.jsonl', run_path(tmp_path, 'Score'))
    assert json.loads((root / 'scores.jsonl').read_text())['total_score'] == 1
    assert len(llm_calls(scored['evidence_run'])) == 1
    audit = tmp_path / 'independent.json'
    full_suite = json.loads((ROOT / 'tests/fixtures/math/suite_draft.json').read_text())
    audit.write_text(json.dumps({'v2_math': full_suite}))
    before = (root / 'trace.jsonl').read_bytes()
    summary = audit_run(root, audit, root / 'audit.json', True, run_path(tmp_path, 'Audit'))
    assert summary['reports'][0]['eligible']
    assert summary['training_selection_affected'] is False
    assert (root / 'trace.jsonl').read_bytes() == before
    audit_calls = llm_calls(summary['evidence_run'])
    assert {c['actor_role'] for c in audit_calls} == {'rubric_judge', 'harness_verifier'}
    assert all(c['split'] == 'audit' for c in audit_calls)


@pytest.mark.acceptance('UAT-16', 'UAT-20', 'UAT-26')
def test_rubric_transport_attempts_share_budget_and_keep_unknown(tmp_path, synthesis):
    adapters = {'rubric_judge': ScriptedLLM([
        Turn(content='{"score":1,"feedback":"correct"}', faults=['read_timeout_after_dispatch']),
        Turn(content='{"score":0,"feedback":"incorrect"}')])}
    root, result = run(tmp_path, synthesis, adapters)
    assert result.status == 'success'
    calls = [c for c in llm_calls(root) if c['actor_role'] == 'rubric_judge']
    assert len(calls) == 3 and calls[0]['dispatch_status'] == 'unknown'
    assert calls[0]['request_digest'] == calls[1]['request_digest']
    stats = report(root)
    assert stats['latest_run_budget']['consumed']['model_requests'] == 8
    assert stats['scoring_requests'] == 3
    assert stats['unknown_usage_events'] == 1


@pytest.mark.acceptance('UAT-12', 'UAT-13')
def test_diagnostic_verifier_failure_does_not_block_required(tmp_path, synthesis, suite_data):
    suite_data['examples'].append({**suite_data['examples'][1], 'id': 'diagnostic_bad', 'response': r'\boxed{3/6}'})
    suite_data['cases'].append({**suite_data['cases'][1], 'id': 'diagnostic', 'example_ids': ['diagnostic_bad'], 'required': False})
    def decisions(request):
        e = json.loads(request.messages[-1].content)['evidence']
        d = base_decision(e, status='error', rationale='diagnostic judge unavailable') if e['case']['id'] == 'diagnostic' else rule_decision(e)
        return Turn(content=d.model_dump_json())
    root, result = run(tmp_path, synthesis, {'test_case_synthesizer': ScriptedLLM([Turn(content=json.dumps(suite_data))]),
                                   'harness_verifier': ScriptedLLM(on_exhausted=decisions)})
    assert result.status == 'success'
    b, _, _, _ = read_run(root)
    assert b.get_json(result.validation_ref)['decisions'][-1]['operation_status'] == 'error'


@pytest.mark.acceptance('UAT-12', 'UAT-13', 'UAT-16', 'UAT-18', 'UAT-20')
@pytest.mark.parametrize('required,fault,first', [
    (False, 'server_error', False),
    (False, 'read_timeout_after_dispatch', True),
    (True, 'server_error', False),
    (True, 'read_timeout_after_dispatch', False),
])
def test_verifier_transport_failure_respects_required_scope(tmp_path, synthesis, suite_data, required, fault, first):
    synthesis.synthesis.model_transport_attempts = 2
    suite_data['examples'].append({**suite_data['examples'][1], 'id': 'unavailable_bad', 'response': r'\boxed{3/6}'})
    case = {**suite_data['cases'][1], 'id': 'unavailable', 'example_ids': ['unavailable_bad'], 'required': required}
    suite_data['cases'].insert(0 if first else len(suite_data['cases']), case)

    def decisions(request):
        evidence = json.loads(request.messages[-1].content)['evidence']
        if evidence['case']['id'] == 'unavailable':
            return Turn(content='', faults=[fault])
        return Turn(content=rule_decision(evidence).model_dump_json())

    root, result = run(tmp_path, synthesis, {
        'test_case_synthesizer': ScriptedLLM([Turn(content=json.dumps(suite_data))]),
        'harness_verifier': ScriptedLLM(on_exhausted=decisions),
    })
    assert result.status == ('failed' if required else 'success')
    assert result.stop_reason == ('infrastructure_error' if required else 'validated')
    calls = llm_calls(root)
    attempts = [c for c in calls if c['actor_role'] == 'harness_verifier' and ':unavailable:' in c['logical_call_id']]
    assert len(attempts) == 2
    assert len({c['request_digest'] for c in attempts}) == 1
    assert all(c['dispatch_status'] == ('unknown' if fault == 'read_timeout_after_dispatch' else 'failed') for c in attempts)
    assert report(root)['latest_run_budget']['consumed']['model_requests'] == len(calls)
    if not required:
        blobs, events, _, _ = read_run(root)
        validation = blobs.get_json(result.validation_ref)
        diagnostic = next(d for d in validation['decisions'] if d['case_id'] == 'unavailable')
        assert diagnostic['operation_status'] == 'error' and diagnostic['passed'] is None
        assert all(d['passed'] is True for d in validation['decisions'] if d['case_id'] != 'unavailable')
        assert any(e.type == 'verification_decision' and e.payload['case_id'] == 'unavailable' for e in events)
        assert export_llm_calls(root, root / 'transport_calls.jsonl')['physical_attempts'] == len(calls)
        assert replay(root)['new_requests'] == 0
        before = (root / 'results.jsonl').read_bytes()
        construct_file(None, None, root, resume=True)
        assert len(llm_calls(root)) == len(calls)
        assert (root / 'results.jsonl').read_bytes() == before

