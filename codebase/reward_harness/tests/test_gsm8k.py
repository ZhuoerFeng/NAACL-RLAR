import pytest

from test_self_contained import execute


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
    assert execute(examples=[{'reference': GOLD, 'response': answer}])[0]['raw_value']['raw_score'] == expected


def test_gsm8k_numeric_formats_and_reference_errors():
    for reference, response in [('1000','#### 1,000.00'),('-3.5','#### -3.50')]:
        assert execute(examples=[{'reference':reference,'response':response}])[0]['raw_value']['raw_score'] == 1
    for reference in (None,'not a reference','#### unknown'):
        assert execute(examples=[{'reference':reference,'response':'#### 72'}])[0]['status'] == 'error'
