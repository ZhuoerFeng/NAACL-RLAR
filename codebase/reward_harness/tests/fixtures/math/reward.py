import re
from fractions import Fraction

def score(example, context):
    matches = re.findall(r"\\boxed\{([^{}]+)\}", example['response'])
    try:
        value = float(bool(matches) and Fraction(matches[-1]) == Fraction(example['reference']))
    except (ValueError, ZeroDivisionError):
        value = 0.0
    return {'raw_score': value, 'feedback': 'Authored source checks the final rational answer.', 'evidence': []}
