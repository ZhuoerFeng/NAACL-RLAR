import pytest

from rlar_harness.evaluation.checkers import CheckerError, Gsm8kNumericCheckerV1, RationalArithmeticCheckerV1
from rlar_harness.runtime.worker import ScoringContext


GOLD = ('Natalia sold 48/2 = <<48/2=24>>24 clips in May. '
        'Natalia sold 48+24 = <<48+24=72>>72 clips altogether in April and May. #### 72')


@pytest.mark.parametrize('answer,expected', [
    (GOLD, 1.0),
    ('48 + 48/2 = 72.\n#### 72.0', 1.0),
    ('#### 24', 0.0),
    ('#### 144', 0.0),
    ('#### 72\nCorrection: #### 24', 0.0),
    ('#### 72 or 24', 0.0),
    ('#### 7,2', 0.0),
    ('#### NaN', 0.0),
    ('#### 72 clips', 0.0),
    (r'\boxed{72}', 0.0),
])
def test_gsm8k_final_answer_contract(answer, expected):
    assert Gsm8kNumericCheckerV1().check_answer({'reference': GOLD, 'response': answer}) == expected


def test_gsm8k_numeric_formats_and_reference_errors():
    checker = Gsm8kNumericCheckerV1()
    assert checker.check_answer({'reference': '1000', 'response': '#### 1,000.00'}) == 1
    assert checker.check_answer({'reference': '-3.5', 'response': '#### -3.50'}) == 1
    for reference in (None, 'not a reference', '#### unknown'):
        with pytest.raises(CheckerError): checker.check_answer({'reference': reference, 'response': '#### 72'})
    assert RationalArithmeticCheckerV1().check_answer({'reference': '72', 'response': '#### 72'}) == 0


def test_gsm8k_bundle_is_available_only_when_declared():
    from rlar_harness.evaluation.checkers import ForbiddenAPI
    context = ScoringContext(['gsm8k_numeric_v1'], ['gsm8k_numeric_v1'], None, 'numeric')
    assert context.check_answer({'reference': GOLD, 'response': '#### 72'}) == 1
    assert context.extract_answer({'response': '#### 72'}) == '72'
    forbidden = ScoringContext(['gsm8k_numeric_v1'], [], None, 'numeric')
    with pytest.raises(ForbiddenAPI): forbidden.check_answer({'reference': GOLD, 'response': '#### 72'})
