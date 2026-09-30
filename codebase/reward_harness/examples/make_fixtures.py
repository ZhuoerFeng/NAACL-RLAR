"""Build explicitly authored offline fixtures and default checker-free profiles."""
import argparse
import shutil
import json
from pathlib import Path
import yaml
from rlar_harness.storage.canonical import digest

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', type=Path, required=True)
out = parser.parse_args().output_dir.resolve()
out.mkdir(parents=True, exist_ok=False)
shutil.copyfile(root / 'examples/self_contained/gsm8k_reward.py', out / 'gsm8k_reward.py')
(out / 'task_packs').mkdir(parents=True, exist_ok=True)


def write(name, data):
    (out / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


query = {'query_id': 'gsm8k_natalia', 'task_profile_id': 'gsm8k_self_contained',
    'query': 'Natalia sold clips to 48 of her friends in April, and then she sold half as many clips in May. How many clips did Natalia sell altogether in April and May?',
    'reference': 'Natalia sold 48/2 = <<48/2=24>>24 clips in May. Natalia sold 48+24 = <<48+24=72>>72 clips altogether in April and May. #### 72'}
(out / 'queries.jsonl').write_text(json.dumps(query) + '\n')
contract = ('GSM8K final-answer correctness. The last #### in the original reference solution gives the gold numeric answer. '
    'A candidate must end in #### followed by one signed integer or decimal; correctly grouped thousands commas are allowed. '
    'No units or trailing prose. Compare values exactly; +72.0 and 72 are equivalent numeric forms. '
    'Do not grade explanation length or claim to certify intermediate reasoning. '
    'Implement extraction, parsing, format validation, comparison and scoring in the generated source using general-purpose Python. '
    'There is no native checker, including during suite admission. Label generated cases model_inferred and cite the task/query. '
    'Verifiable components do not call context APIs. Rubric components may call the declared raw LLM utility and must parse it themselves. '
    'Use reusable logic, not hardcoded answers, question strings or case IDs.')
pack = json.loads((root / 'examples/self_contained/task_packs/gsm8k_self_contained.json').read_text())
pack.update(profile_id='gsm8k_self_contained', version='2', task_contract=contract,
    reward_logic_policy='self_contained_v1', permitted_apis=['call_llm_api'], resources={},
    capabilities=[{'id': 'final_answer_correctness', 'description': 'Final numeric answer matches the authorized reference.',
                   'task_requirement_ref': 'gsm8k_self_contained'}])
pack['applicability_rule']['task_contract_digest'] = digest(contract)
pack['suite_policy'].update(objective_checker=None, required_sources=['model_inferred', 'human_annotated'])
write('task_packs/gsm8k_self_contained.json', pack)

pairs = [('worked', 'April is 48. May is 24. Altogether 48 + 24 = 72. #### 72', 1),
         ('decimal', 'Half of 48 is 24, so the total is 72. #### +72.0', 1),
         ('brief', '#### 72', 1), ('may_only', 'May sales are 48/2 = 24. #### 24', 0),
         ('april_only', 'The total is the April sales. #### 48', 0),
         ('doubled', 'May sales equal April sales. #### 96', 0)]
suite = {'schema_version': 'rlar.suite_draft.v2', 'id': 'gsm8k_authored_fixture', 'version': '1',
    'capabilities': pack['capabilities'], 'examples': [], 'cases': []}
base = {'capability_ids': ['final_answer_correctness'], 'scope': 'capability', 'required': True,
        'evidence_source': 'model_inferred', 'evidence_refs': [query['query_id']],
        'justification': 'Authored engineering fixture; proposed numerical label, no native checker certification.'}
for name, response, label in pairs:
    suite['examples'].append({'id': name, 'query_ref': query['query_id'], 'response': response,
                             'provenance': 'authored_offline_engineering_fixture'})
    suite['cases'].append({**base, 'id': 'point_' + name, 'kind': 'pointwise', 'example_ids': [name],
        'expected_label': 'pass' if label else 'fail', 'discrimination_target': 'Final numeric correctness: ' + name,
        'expected_behavior': 'Accept the correct final number.' if label else 'Penalize the incorrect final number.'})
for good, bad in [('worked', 'may_only'), ('decimal', 'april_only'), ('brief', 'doubled')]:
    suite['cases'].append({**base, 'id': 'rank_' + good, 'kind': 'ranking', 'example_ids': [good, bad],
        'discrimination_target': 'Correct final number versus incorrect final number.', 'expected_behavior': 'Rank the correct answer higher.',
        'relations': [{'left': good, 'right': bad, 'operator': '>', 'justification': 'Proposed reference-based comparison.', 'evidence_refs': [query['query_id']]}]})
write('suite_draft.json', suite)
write('test_responses.json', {'default': [{'content': json.dumps(suite)}]})
rubric_source = '''import json
import math

def parse_judge(raw):
    data = json.loads(raw)
    value = data['score']
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value not in (0, 1):
        raise ValueError('Expected a finite binary numeric score')
    if not isinstance(data['feedback'], str):
        raise ValueError('Expected textual feedback')
    return {'raw_score': float(value), 'feedback': data['feedback'], 'evidence': []}

def score(example, context):
    message = json.dumps({**example, 'rubric': context.judge_spec['prompt_template']})
    return parse_judge(context.call_llm_api(message, context.judge_spec['model_ref']))
'''
definition = {'schema_version': 'rlar.reward.v2', 'mode': 'checklist', 'aggregation': {'kind': 'mean'},
    'capabilities': pack['capabilities'], 'runtime_contract': {'scoring_abi': 'v2',
        'environment_ref': pack['applicability_rule']['runtime_fingerprint'], 'reward_logic_policy': 'self_contained_v1'},
    'components': [{'id': 'numeric', 'kind': 'verifiable', 'capability_ids': ['final_answer_correctness'],
        'criterion': 'Exact final numeric correctness.', 'normalization': {'kind': 'identity'},
        'required_apis': [], 'source': (out / 'gsm8k_reward.py').read_text()},
        {'id': 'rubric', 'kind': 'rubric', 'capability_ids': ['final_answer_correctness'],
         'criterion': 'Reference-based final-answer correctness.', 'normalization': {'kind': 'identity'},
         'required_apis': ['call_llm_api'], 'source': rubric_source,
         'judge_spec': {'model_ref': 'rubric', 'model_config_digest': '$JUDGE_CONFIG_DIGEST',
            'prompt_template': 'Check only final numeric correctness against the original reference. Require the declared #### format; equivalent signed decimals are correct. Return JSON with score 0 or 1 and textual feedback.',
            'output_contract': 'JSON object with numeric score and textual feedback.', 'parser_entrypoint': 'parse_judge'}}]}
write('reward_responses.json', {'default': [{'actions': [{'id': 'test', 'tool': 'test_reward',
    'arguments': {'definition': definition, 'dev_profile_id': pack['profile_id']}}]}]})
write('verifier_responses.json', {'fixture': 'self_contained_verifier', 'expected_scores': {n: s for n, _, s in pairs}})
write('rubric_responses.json', {'fixture': 'self_contained_rubric', 'responses': {r: {'score': s, 'feedback': 'Authored offline rubric result.'} for _, r, s in pairs}})

cfg = yaml.safe_load((root / 'configs/offline.yaml').read_text())
cfg['data'].update(input_path='queries.jsonl', task_pack_root='task_packs')
cfg['validation']['dev_suite_root'] = '.'
for model in cfg['models'].values():
    model['scripted_responses'] = Path(model['scripted_responses']).name
(out / 'offline.yaml').write_text(yaml.safe_dump(cfg, sort_keys=False))
print(out)
