import json

from rlar_harness.llm.protocol import parse_actions

TOOLS = {'read_resource', 'test_reward', 'submit_reward'}
ENVELOPE = {'actions': [{'id': 'a1', 'tool': 'test_reward', 'arguments': {
    'definition': {'components': [{'id': 'c1', 'source': 'def score(example, context):\n    return {}\n'}]}}}]}


def test_missing_closing_brace_reports_decode_position_not_missing_key():
    text = json.dumps(ENVELOPE, separators=(',', ':'))
    broken = text[:-3] + text[-2:]  # drop one '}' before the final ']}'
    error = parse_actions(broken, known_tools=TOOLS).error
    assert error.code == 'invalid_action_schema'
    assert "no 'actions' key" not in error.message
    assert 'is not valid JSON' in error.message and f'(char {len(broken) - 2})' in error.message
    assert 'bracket mismatch' in error.message and '<HERE>' in error.message


def test_unterminated_envelope_reports_unclosed_brackets():
    text = json.dumps(ENVELOPE)[:-4]
    message = parse_actions(text, known_tools=TOOLS).error.message
    assert 'is not valid JSON' in message and 'never closed' in message


def test_prose_braces_and_valid_envelopes_keep_previous_behaviour():
    valid = json.dumps(ENVELOPE)
    assert parse_actions('Plan {draft} first.\n' + valid, known_tools=TOOLS).ok
    assert "no 'actions' key" in parse_actions('{"tool": "test_reward"}', known_tools=TOOLS).error.message
    assert 'no parseable JSON' in parse_actions('use {x} here', known_tools=TOOLS).error.message
