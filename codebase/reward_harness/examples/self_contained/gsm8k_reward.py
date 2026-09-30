"""Authored engineering fixture, never provided as a real synthesis prompt."""
import re
from fractions import Fraction


def numeric(text):
    if not isinstance(text, str):
        return None
    value = text.strip()
    if not re.fullmatch(r'[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?', value):
        return None
    return Fraction(value.replace(',', ''))


def score(example, context):
    reference = example.get('reference')
    if not isinstance(reference, str):
        raise ValueError('Reference must be the declared solution text or numeric string')
    expected = numeric(reference.rsplit('####', 1)[-1])
    if expected is None:
        raise ValueError('Reference has no valid final numeric answer')
    response = example.get('response')
    answer = numeric(response.rsplit('####', 1)[-1]) if isinstance(response, str) and '####' in response else None
    correct = answer is not None and answer == expected
    return {'raw_score': float(correct), 'feedback': 'Final number matches.' if correct else 'Final answer is invalid or incorrect.',
            'evidence': [{'kind': 'numeric_comparison', 'candidate': str(answer), 'reference': str(expected)}]}
