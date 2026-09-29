"""Bounded acceptance tools. No execution, mutation, or finalization authority."""
import json

from ..errors import HarnessError
from ..llm.history import EpisodeHistory
from ..schemas import Message, ValidationDecision
from ..storage.canonical import digest


def tool_history(instructions, payload):
    return EpisodeHistory((Message(role='system', actor='run_prefix', content=instructions),),
        (Message(role='user', actor='episode_prefix', content=json.dumps(payload, ensure_ascii=False)),))


def evidence_for(case, suite, definition, scores, pack, verifier):
    selected = [s.model_dump(mode='json') for s in scores if s.example_id in case.example_ids]
    evidence = {'task_contract': pack.task_contract, 'capabilities': [c.model_dump(mode='json') for c in suite.capabilities],
        'case': case.model_dump(mode='json'),
        'examples': [e.model_dump(mode='json') for e in suite.examples if e.id in case.example_ids],
        'components': [{'id': c.id, 'capability_ids': c.capability_ids, 'criterion': c.criterion} for c in definition.components],
        'scores': selected, 'reward_key': scores[0].reward_key,
        'suite_digest': suite.suite_digest, 'verifier_config_digest': verifier.config_digest,
        'verifier_model_ref': verifier.model_ref, 'verifier_prompt_version': verifier.config.verifier_prompt_version}
    evidence['score_differences'] = [{
        'left': r.left, 'right': r.right,
        'components': {c.id: _difference(scores, r.left, r.right, c.id) for c in definition.components},
        'total': _difference(scores, r.left, r.right, None)} for r in case.relations]
    evidence['evidence_digest'] = digest(evidence)
    evidence['available_evidence_refs'] = [f"{s['example_id']}/{c['id']}" for s in selected for c in s['component_results']]
    return evidence


def _difference(scores, left, right, component):
    def value(id):
        s = next(s for s in scores if s.example_id == id)
        return s.total_score if component is None else next(c.score for c in s.component_results if c.id == component)
    a, b = value(left), value(right)
    return a - b if a is not None and b is not None else None


def base_decision(evidence, *, status='completed', passed=None, relevant=None, rationale='', repair=''):
    ids = relevant if relevant is not None else [c['id'] for c in evidence['components']
        if set(c['capability_ids']) & set(evidence['case']['capability_ids'])]
    return ValidationDecision(case_id=evidence['case']['id'], reward_key=evidence['reward_key'],
        suite_digest=evidence['suite_digest'], evidence_digest=evidence['evidence_digest'],
        operation_status=status, passed=passed, relevant_component_ids=ids,
        evidence_refs=[ref for ref in evidence['available_evidence_refs'] if ref.rsplit('/', 1)[-1] in ids],
        rationale=rationale, repair_feedback=repair, verifier_model_ref=evidence['verifier_model_ref'],
        verifier_prompt_version=evidence['verifier_prompt_version'])


def check_decision(decision, evidence):
    for key, value in {'case_id': evidence['case']['id'], **{k: evidence[k] for k in
        ('reward_key', 'suite_digest', 'evidence_digest', 'verifier_model_ref', 'verifier_prompt_version')}}.items():
        if getattr(decision, key) != value:
            raise ValueError('verifier binding mismatch: ' + key)
    ids = set(decision.relevant_component_ids)
    known = {c['id'] for c in evidence['components']}
    if not ids <= known or len(ids) != len(decision.relevant_component_ids):
        raise ValueError('verifier references nonexistent/repeated components')
    if not set(decision.evidence_refs) <= set(evidence['available_evidence_refs']):
        raise ValueError('verifier references nonexistent execution evidence')
    if decision.operation_status != 'completed':
        return
    if not ids:
        raise ValueError('completed decision needs actual numerical component evidence')
    declared = {c['id'] for c in evidence['components']
                if set(c['capability_ids']) & set(evidence['case']['capability_ids'])}
    required = known if evidence['case']['scope'] == 'overall' else ids | declared
    for score in evidence['scores']:
        if any(c['status'] != 'ok' or c['score'] is None for c in score['component_results'] if c['id'] in required):
            raise ValueError('completed verdict uses missing required score evidence')
        if not {f"{score['example_id']}/{id}" for id in ids} <= set(decision.evidence_refs):
            raise ValueError('decision must cite actual observations for every relevant candidate/component')


def rule_decision(evidence, threshold=0.5, tolerance=1e-9):
    case = evidence['case']
    if case['evidence_source'] not in ('objective_verified', 'human_annotated'):
        return base_decision(evidence, status='insufficient_evidence', rationale='rule baseline requires independent labels')
    ids = {c['id'] for c in evidence['components'] if set(c['capability_ids']) & set(case['capability_ids'])}
    if case['scope'] == 'overall':
        ids = {c['id'] for c in evidence['components']}
    if not ids:
        return base_decision(evidence, status='insufficient_evidence', rationale='no declared capability mapping')
    values = {}
    for score in evidence['scores']:
        parts = [c for c in score['component_results'] if c['id'] in ids]
        if any(c['status'] != 'ok' or c['score'] is None for c in parts):
            return base_decision(evidence, status='insufficient_evidence', rationale='required component did not execute')
        values[score['example_id']] = sum(c['score'] for c in parts) / len(parts)
    if case['kind'] == 'pointwise':
        passed = (values[case['example_ids'][0]] >= threshold) == (case['expected_label'] == 'pass')
    else:
        passed = all(values[r['left']] > values[r['right']] + tolerance if r['operator'] == '>'
            else abs(values[r['left']] - values[r['right']]) <= tolerance for r in case['relations'])
    return base_decision(evidence, passed=passed, relevant=sorted(ids), rationale='fixed capability-level numerical rule',
                         repair='' if passed else 'Repair numerical behavior on the declared capability scope.')


class HarnessVerifier:
    def __init__(self, config, client=None, *, model_ref='rule_baseline', model_config=None):
        self.config, self.client, self.model_ref = config, client, model_ref
        self.config_digest = digest({'config': config, 'model': model_config})

    def verify(self, evidence, operation_id):
        if len(json.dumps(evidence, ensure_ascii=False)) > self.config.max_verifier_input_chars:
            return base_decision(evidence, status='error', rationale='verifier input limit exceeded')
        known = {c['id'] for c in evidence['components']}
        declared = {c['id'] for c in evidence['components'] if set(c['capability_ids']) & set(evidence['case']['capability_ids'])}
        required = known if evidence['case']['scope'] == 'overall' else declared
        if any(c['status'] != 'ok' for s in evidence['scores'] for c in s['component_results'] if c['id'] in required):
            return base_decision(evidence, status='insufficient_evidence', rationale='required score evidence incomplete')
        if self.config.backend == 'rule_baseline':
            result = rule_decision(evidence, self.config.rule_threshold, self.config.tie_tolerance)
            check_decision(result, evidence)
            return result
        error = None
        for attempt in range(self.config.verification_format_attempts):
            logical = f'{operation_id}:verifier:{attempt + 1}'
            payload = {'evidence': evidence, 'output_schema': ValidationDecision.model_json_schema(),
                       'format_error': error}
            history = tool_history(self.config.verifier_instructions, payload)
            self.client.budget.debit_once(logical + ':tool', {'tool_calls': 1.0})
            response = self.client.call(history, logical_call_id=logical)
            if response.status in ('failed', 'unknown'):
                raise HarnessError('verifier service unavailable', code='environment_unavailable')
            try:
                if response.status != 'complete':
                    raise ValueError('truncated verifier output')
                result = ValidationDecision.model_validate_json(response.assistant_text)
                check_decision(result, evidence)
            except ValueError as exc:
                error = str(exc)[:4000]
                self.client.accept(history, response, logical, schema_valid=False)
                continue
            self.client.accept(history, response, logical, schema_valid=True)
            return result  # A valid false/null is final for this evidence, never sampled again.
        return base_decision(evidence, status='error', rationale='invalid verifier output: ' + str(error))
