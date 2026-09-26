"""Reproducible, synthetic fixtures only; never reads the project dataset."""
from pathlib import Path
import json
from fractions import Fraction
from rlar_harness.storage.canonical import digest
from rlar_harness.schemas import AcceptancePolicy

ROOT = Path(__file__).parent

def write(path, obj):
    (ROOT / path).write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n')


def definition(code, api, mode='checklist'):
    return {'mode': mode, 'components': [{'id': 'correctness', 'criterion': 'Task contract correctness',
        'source': code, 'normalization': {'kind': 'identity'}, 'required_apis': [api]}],
        'aggregation': {'kind': 'mean' if mode == 'checklist' else 'identity'},
        'runtime_contract': {'environment_ref': 'python-stdlib-fixture-v1'}}


def action(defn):
    return {'actions': [{'id': 'test', 'tool': 'test_reward', 'arguments': {'definition': defn,
        'dev_profile_id': 'math_v1' if defn['components'][0]['required_apis'] == ['rational_arithmetic_v1'] else 'code_v1',
        'on_pass': 'submit', 'display_limit': 2}}]}


def main():
    for name, api, contract, inputs in (
        ('math_v1', 'rational_arithmetic_v1', 'Exact rational arithmetic; final answer must appear in \\boxed{...}.', ['query', 'response', 'reference']),
        ('code_v1', 'python_unit_tests_v1', 'Implement abs_int(n) for integers; meet every supplied boundary test.', ['query', 'response', 'task_tests', 'task_entrypoint']),
    ):
        write('task_packs/' + name + '.json', {'schema_version': 'rlar.taskpack.v1', 'profile_id': name, 'version': '1',
            'task_contract': contract, 'applicability_rule': {'task_contract_digest': digest(contract),
                'permitted_inputs': inputs, 'mode_constraints': ['single', 'checklist'], 'runtime_fingerprint': 'python-stdlib-fixture-v1'},
            'permitted_inputs': inputs, 'permitted_apis': [api], 'mode_constraints': ['single', 'checklist'],
            'dev_suite_ref': name, 'verifier_version': 'fixture-oracle-v1',
            'acceptance_policy': AcceptancePolicy().model_dump(), 'max_components': 4,
            'resources': {'checker_help': 'Use context.check_answer(example)' if name == 'math_v1' else 'Use context.run_task_tests(example)'}})
    cases = []
    for idx, (a, b) in enumerate([(Fraction(1,2), Fraction(1,3)), (Fraction(-3,4), Fraction(1,8))]):
        expected = a + b  # Independent fixture oracle, not the reward parser.
        for variant, ans, label, kind in [('yes', str(expected), True, 'correct'),
            ('no', str(expected + 1), False, 'incorrect'),
            ('equivalent', f'{expected.numerator*2}/{expected.denominator*2}', True, 'invariance')]:
            cases.append({'case_id': f'm{idx}_{variant}', 'kind': kind, 'is_correct': label,
                'group_id': f'm{idx}', 'base_case_id': f'm{idx}_yes' if kind == 'invariance' else None,
                'example': {'query': f'Compute {a} + {b}', 'response': '\\boxed{' + ans + '}', 'reference': str(expected)}})
    write('dev_suites/math_v1.json', {'suite_id': 'math_v1', 'version': '1', 'cases': cases})
    tests = [{'args': [n], 'expected': abs(n)} for n in [-17, -1, 0, 1, 42]]
    codecases = []
    for name, source, label, kind in [('yes', 'def abs_int(n):\n    return abs(n)\n', True, 'correct'),
        ('no', 'def abs_int(n):\n    return n\n', False, 'incorrect'),
        ('boundary', 'def abs_int(n):\n    return 1 if n == 0 else abs(n)\n', False, 'perturbation'),
        ('equivalent', 'def abs_int(n):\n    return n if n >= 0 else -n\n', True, 'invariance')]:
        codecases.append({'case_id': name, 'kind': kind, 'is_correct': label, 'group_id': 'abs',
            'base_case_id': 'yes' if kind == 'invariance' else None,
            'example': {'query': 'Implement abs_int(n)', 'response': source, 'task_tests': tests, 'task_entrypoint': 'abs_int'}})
    # Reward is binary all-tests-pass; negative candidates with some passes stay negative.
    write('dev_suites/code_v1.json', {'suite_id': 'code_v1', 'version': '1', 'cases': codecases})
    for name in ('math_v1', 'code_v1'):
        suite = json.loads((ROOT / 'dev_suites' / (name + '.json')).read_text())
        suite['suite_id'] = 'audit_' + name
        suite['version'] = 'prototype-1'
        if name == 'math_v1':
            for c in suite['cases']:
                c['example']['query'] = 'Compute 7/9 + 2/9'
                c['example']['reference'] = '1'
                c['example']['response'] = '\\boxed{2/2}' if c['is_correct'] else '\\boxed{2}'
        else:
            for c in suite['cases']:
                c['example']['task_tests'] = [{'args': [n], 'expected': abs(n)} for n in [-999, 0, 999]]
        write('audit_suites/' + name + '.json', suite)
    write('audit_suite.json', {'math_v1': 'math_v1', 'code_v1': 'code_v1'})
    good = definition('def score(example, context):\n    return context.check_answer(example)\n', 'rational_arithmetic_v1')
    bad = definition('def score(example, context):\n    return 1.0\n', 'rational_arithmetic_v1')
    code = definition('def score(example, context):\n    return float(context.run_task_tests(example) == 1.0)\n', 'python_unit_tests_v1')
    write('scripted_responses.json', {'math_v1': [action(bad), action(good)], 'code_v1': [action(code)]})
    records = [{'query_id': 'math_001', 'query': 'Compute 1/2 + 1/3', 'task_profile_id': 'math_v1', 'reference': '5/6'},
               {'query_id': 'math_002', 'query': 'Compute 3/5 + 1/5', 'task_profile_id': 'math_v1', 'reference': '4/5'},
               {'query_id': 'code_001', 'query': 'Implement abs_int(n)', 'task_profile_id': 'code_v1',
                'metadata': {'task_tests': tests, 'task_entrypoint': 'abs_int'}}]
    (ROOT / 'queries.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
    candidates = [{'query_id': 'math_001', 'response': '\\boxed{5/6}', 'candidate_id': 'positive'},
        {'query_id': 'math_001', 'response': '\\boxed{1}', 'candidate_id': 'negative'},
        {'query_id': 'code_001', 'response': 'def abs_int(n):\n    return abs(n)\n', 'candidate_id': 'code'}]
    (ROOT / 'candidates.jsonl').write_text(''.join(json.dumps(c) + '\n' for c in candidates))

if __name__ == '__main__':
    main()
