import json

import pytest

from test_synthesis import ROOT, admission, reward, reward_turn, run, suite_data, synthesis  # noqa: F401
from rlar_harness.evaluation.suite import _check_order, _implied_edges
from rlar_harness.llm.scripted import ScriptedLLM
from rlar_harness.schemas import SuiteDraft, ToolResult
from rlar_harness.trace.export import read_run


def test_ranking_against_pointwise_labels_is_rejected_with_chain(tmp_path, synthesis, suite_data):
    rank = suite_data['cases'][-1]
    good, bad = rank['relations'][0]['left'], rank['relations'][0]['right']
    rank['relations'][0].update(left=bad, right=good)
    with pytest.raises(ValueError, match='implied by labels') as info:
        admission(tmp_path, suite_data, synthesis)
    assert 'strict preference cycle' in str(info.value) and f'{bad} > {good}' in str(info.value)


def test_overall_ranking_against_pareto_dominance_is_rejected():
    caps = {'correct', 'format'}
    labels = {('capability', 'correct', 'a'): 'pass', ('capability', 'format', 'a'): 'pass',
              ('capability', 'correct', 'b'): 'fail', ('capability', 'format', 'b'): 'pass',
              ('capability', 'correct', 'c'): 'pass', ('capability', 'format', 'c'): 'fail'}
    implied = _implied_edges({('overall', 'correct'): []}, labels, caps)['overall', None]
    assert {(l, r) for l, _, r, _ in implied} == {('a', 'b'), ('a', 'c')}  # b and c are incomparable
    with pytest.raises(ValueError, match='overall'):
        _check_order([('b', '>', 'a', 'case x')] + implied, 'overall ranking')
    with pytest.raises(ValueError, match='declared equivalent'):
        _check_order([('a', '=', 'b', 'case y')] + implied, 'overall ranking')
    # A priority the labels do not decide (correctness over format) stays expressible.
    _check_order([('c', '>', 'b', 'case z')] + implied, 'overall ranking')


def test_violation_feedback_is_optional_and_digest_neutral(suite_data):
    plain = SuiteDraft.model_validate(suite_data)
    assert 'violation_feedback' not in plain.model_dump(mode='json')['cases'][0]
    suite_data['cases'][0]['violation_feedback'] = 'A reward that rejects the correct answer over-penalizes.'
    assert SuiteDraft.model_validate(suite_data).cases[0].violation_feedback.startswith('A reward')


def test_failed_cases_are_returned_with_intent_and_actual_scores(tmp_path, synthesis):
    synthesis.synthesis.reward_synthesis_attempts = 1
    d = reward(synthesis)
    d['components'] = [d['components'][0]]
    d['components'][0]['source'] = 'def score(example, context):\n    return {"raw_score": 1.0, "feedback": "constant", "evidence": []}'
    root, result = run(tmp_path, synthesis, {'reward_synthesizer': ScriptedLLM([reward_turn(d)])})
    assert result.status == 'failed'
    blobs, events, _, _ = read_run(root)
    outputs = [ToolResult.model_validate(blobs.get_json(e.payload['result_ref'])) for e in events if e.type == 'tool_result']
    tested = next(o.result for o in outputs if o.tool == 'test_reward')
    unmet = tested['unmet_cases']
    assert unmet and tested['unmet_cases_resource'].startswith('unmet_cases:')
    case = next(c for c in unmet if c['expected_label'] == 'fail')
    assert case['expected_behavior'] and case['justification'] and 'violation_feedback' in case
    (observed,) = case['observed'].values()
    assert observed['total_score'] == 1.0 and observed['response']
    assert all(c['passed'] is not True for c in unmet)


def test_unavailable_model_evidence_ref_names_offending_and_allowed_ids(tmp_path, synthesis, suite_data):
    case = next(c for c in suite_data['cases'] if c['evidence_source'] == 'model_inferred')
    case['evidence_refs'] = ['query:not-a-ref']
    with pytest.raises(ValueError, match='unavailable task evidence') as info:
        admission(tmp_path, suite_data, synthesis)
    assert "['query:not-a-ref']" in str(info.value) and 'must be exact ids from' in str(info.value)


@pytest.mark.parametrize('feedback', ['full', 'scores_only'])
def test_component_return_errors_are_reported_ahead_of_decisions(tmp_path, synthesis, feedback):
    synthesis.synthesis.reward_synthesis_attempts = 1
    synthesis.synthesis.feedback = feedback
    d = reward(synthesis)
    d['components'] = [d['components'][0]]
    d['components'][0]['source'] = 'def score(example, context):\n    return {"raw_score": 1.0, "feedback": "x", "evidence": ["text"]}'
    root, _ = run(tmp_path, synthesis, {'reward_synthesizer': ScriptedLLM([reward_turn(d)])})
    blobs, events, _, _ = read_run(root)
    outputs = [ToolResult.model_validate(blobs.get_json(e.payload['result_ref'])) for e in events if e.type == 'tool_result']
    tested = next(o.result for o in outputs if o.tool == 'test_reward')
    (errors,) = tested['component_errors'].values()
    entry = errors['component_return_type']
    assert entry['count'] == tested['complete_case_count']
    (observed,) = tested['unmet_cases'][0]['observed'].values()
    (component,) = observed['components'].values()
    assert component['error'] == 'component_return_type'
    if feedback == 'full':
        assert 'object list' in entry['message'] and 'object list' in component['message']
    else:
        assert 'message' not in entry and 'message' not in component
    rendered = json.dumps(tested, sort_keys=True)
    assert rendered.index('"component_errors"') < rendered.index('"decisions"')
