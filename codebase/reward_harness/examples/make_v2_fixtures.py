"""Regenerate explicitly offline v2 engineering fixtures (no model requests)."""
import json
from pathlib import Path
import yaml

root = Path(__file__).resolve().parents[1]
ex = root / 'examples/v2'
(ex / 'task_packs').mkdir(parents=True, exist_ok=True)

def write(name, obj):
    (ex / name).write_text(json.dumps(obj, indent=2) + '\n')

pack = json.loads((root / 'examples/task_packs/math_v1.json').read_text())
pack.update(schema_version='rlar.taskpack.v2', profile_id='math_v2', dev_suite_ref=None,
    capabilities=[{'id': 'correctness', 'description': 'Exact rational answer correctness', 'task_requirement_ref': 'math_v2'}],
    suite_policy={'ranking_layout': 'pairwise', 'categories': ['fail', 'pass', 'ranking'],
        'required_sources': ['objective_verified'], 'min_cases_per_category': 1, 'max_cases': 12,
        'max_examples': 8, 'objective_checker': 'rational_arithmetic_v1:check_answer'})
pack['permitted_apis'].append('call_llm_api')
write('task_packs/math_v2.json', pack)
query = {'query_id': 'v2_math', 'query': 'Compute 1/2 + 1/3', 'reference': '5/6', 'task_profile_id': 'math_v2'}
(ex / 'queries.jsonl').write_text(json.dumps(query) + '\n')
ref = 'checker:rational_arithmetic_v1:check_answer'
suite = {'schema_version': 'rlar.suite_draft.v2', 'id': 'math_generated', 'version': '1',
    'capabilities': pack['capabilities'], 'examples': [{'id': id, 'query_ref': 'v2_math', 'response': r,
    'provenance': 'Offline fixture authored for engineering checks'} for id, r in [('good', r'\boxed{5/6}'), ('bad', r'\boxed{1/6}')]], 'cases': []}
base = {'capability_ids': ['correctness'], 'scope': 'capability', 'required': True,
    'justification': 'Independent exact rational arithmetic checker', 'evidence_source': 'objective_verified', 'evidence_refs': [ref]}
for id, label in [('good', 'pass'), ('bad', 'fail')]:
    suite['cases'].append({**base, 'id': label, 'kind': 'pointwise', 'example_ids': [id], 'expected_label': label,
        'discrimination_target': 'Exact arithmetic correctness', 'expected_behavior': 'Retain correct answers' if label == 'pass' else 'Penalize incorrect answers'})
suite['cases'].append({**base, 'id': 'ranking', 'kind': 'ranking', 'example_ids': ['good', 'bad'],
    'discrimination_target': 'Prefer correct rational answers',
    'expected_behavior': 'Correct answer scores above incorrect answer in correctness capability',
    'relations': [{'left': 'good', 'right': 'bad', 'operator': '>', 'justification': 'Only good equals the independently computed reference', 'evidence_refs': [ref]}]})
write('suite_draft.json', suite)
write('test_responses.json', {'default': [{'content': json.dumps(suite)}]})
definition = {'schema_version': 'rlar.reward.v2', 'mode': 'checklist', 'capabilities': pack['capabilities'],
    'aggregation': {'kind': 'mean'}, 'runtime_contract': {'scoring_abi': 'v2', 'environment_ref': pack['applicability_rule']['runtime_fingerprint']},
    'components': [
        {'id': 'exact', 'kind': 'verifiable', 'capability_ids': ['correctness'], 'criterion': 'Exact arithmetic correctness',
         'normalization': {'kind': 'identity'}, 'required_apis': ['rational_arithmetic_v1'],
         'source': 'def score(example, context):\n    result = context.check_answer(example)\n    return {"raw_score": result, "feedback": "Exact arithmetic check: " + str(result), "evidence": []}\n'},
        {'id': 'rubric', 'kind': 'rubric', 'capability_ids': ['correctness'], 'criterion': 'Rubric assesses correctness',
         'normalization': {'kind': 'identity'}, 'required_apis': ['call_llm_api'],
         'judge_spec': {'model_ref': 'rubric', 'model_config_digest': '$JUDGE_CONFIG_DIGEST',
             'prompt_template': 'Judge exact arithmetic correctness. Respond with JSON score in [0,1] and feedback.',
             'output_contract': '{"score": float, "feedback": string}', 'parser_entrypoint': 'parse_judge'},
         'source': 'import json\ndef parse_judge(raw):\n    obj = json.loads(raw)\n    return {"raw_score": obj["score"], "feedback": obj["feedback"], "evidence": []}\ndef score(example, context):\n    message = json.dumps({**example, "rubric": context.judge_spec["prompt_template"]})\n    return parse_judge(context.call_llm_api(message, context.judge_spec["model_ref"]))\n'}]}
write('reward_responses.json', {'default': [{'actions': [{'id': 'test', 'tool': 'test_reward', 'arguments': {'definition': definition, 'dev_profile_id': 'math_v2'}}]}]})
for name in ('verifier', 'rubric'):
    write(name + '_responses.json', {'fixture': name})
c = yaml.safe_load((root / 'configs/offline_demo.yaml').read_text())
c.update(schema_version='rlar.config.v2', run_label='v2-offline-engineering-fixture')
c['data'].update(input_path='../examples/v2/queries.jsonl', task_pack_root='../examples/v2/task_packs')
c['validation'].update(dev_suite_root='../examples/v2', audit_suite_root=None)
model = c.pop('model')
model['max_output_tokens'] = 8000
c['models'] = {k: {**model, 'model': 'offline-' + k, 'scripted_responses': '../examples/v2/' + file + '_responses.json'}
               for k, file in [('base_tests', 'test'), ('base_rewards', 'reward'), ('verifier', 'verifier'), ('rubric', 'rubric')]}
c['roles'] = {'test_case_synthesizer': 'base_tests', 'reward_synthesizer': 'base_rewards', 'harness_verifier': 'verifier', 'rubric_judge': 'rubric'}
c['v2'] = {'backend': 'semantic_verifier', 'feedback': 'full'}
for scope in ('run', 'episode'):
    c['budget'][scope].update(controller_steps=30, revisions=20, tool_calls=100, test_cases=200,
        component_executions=500, model_requests=100, scoring_requests=50, input_tokens=3000000, output_tokens=500000)
c['execution']['wall_timeout_s'] = 10
c['budget']['per_tool_timeout_s'] = 120
(root / 'configs/offline_v2.yaml').write_text(yaml.safe_dump(c, sort_keys=False))
