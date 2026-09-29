import json
from pathlib import Path

import httpx
import pytest

from rlar_harness.config import ModelConfig, load_config, preflight
from rlar_harness.driver import construct_file, resolve_config
from rlar_harness.evaluation.checkers import RationalArithmeticCheckerV1
from rlar_harness.evaluation.verifier import rule_decision
from rlar_harness.llm.adapter import ProviderError
from rlar_harness.llm.http_responses_json_v1 import HttpResponsesJsonV1
from rlar_harness.storage.canonical import digest
from rlar_harness.trace.export import export_llm_calls, export_sft, llm_calls, replay
from rlar_harness.trace.report import report
from test_services import server

ROOT = Path(__file__).resolve().parents[1]


def response(text='{"score":1,"feedback":"correct"}'):
    return {'id': 'resp-fixture', 'status': 'completed', 'error': None,
        'output': [{'type': 'reasoning', 'summary': []},
                   {'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
        'usage': {'input_tokens': 40, 'output_tokens': 20,
                  'input_tokens_details': {'cached_tokens': 10}}}


def responses_model(**kwargs):
    return ModelConfig(provider_adapter='http_responses_json_v1', endpoint='https://example.invalid/responses',
                       model='gpt-4.1', **kwargs)


@pytest.mark.acceptance('UAT-04', 'UAT-18', 'UAT-20', 'UAT-21', 'UAT-22', 'UAT-26')
def test_responses_wire_retry_four_roles_exports_and_resume(tmp_path, monkeypatch):
    config = resolve_config(load_config(ROOT / 'configs/offline_v2.yaml'), ROOT / 'configs')
    config.execution.allow_untrusted_code = True
    suite = (ROOT / 'examples/v2/suite_draft.json').read_text()
    action = json.loads((ROOT / 'examples/v2/reward_responses.json').read_text())['default'][0]
    secret = 'responses-fixture-key-never-persist'
    monkeypatch.setenv('RESPONSES_TEST_KEY', secret)

    def answer(body):
        role = next(role for role, ref in config.roles.items() if ref == body['model'])
        if role == 'test_case_synthesizer':
            text = suite
        elif role == 'reward_synthesizer':
            text = json.dumps(action)
        elif role == 'harness_verifier':
            evidence = json.loads(body['input'][-1]['content'])['evidence']
            text = rule_decision(evidence).model_dump_json()
        else:
            example = json.loads(json.loads(body['input'][-1]['content']))
            text = json.dumps({'score': RationalArithmeticCheckerV1().check_answer(example), 'feedback': 'fixture judge'})
        return 200, response(text)

    with server([(503, {'error': 'temporary'}), answer]) as (url, observed):
        for ref, model in config.models.items():
            config.models[ref] = model.model_copy(update={
                'provider_adapter': 'http_responses_json_v1', 'endpoint': url,
                'model': ref, 'api_key_env': 'RESPONSES_TEST_KEY', 'auth_provider': 'azure',
                'response_format': 'json_object', 'adapter_version': 'http_responses_json_v1',
                'reasoning_effort': 'low', 'prompt_cache_key': 'responses-test', 'top_p': 1.0})
        action['actions'][0]['arguments']['definition']['components'][1]['judge_spec']['model_config_digest'] = digest(config.role_model('rubric_judge'))
        root = tmp_path / '2026_09_29_00_00_Responses'
        rows = construct_file(config, config.data.input_path, root)
        assert rows[0].status == 'success'
        calls = llm_calls(root)
        assert len(calls) == len(observed) == 8
        assert calls[0]['dispatch_status'] == 'unknown'
        assert calls[0]['request_digest'] == calls[1]['request_digest']
        for call, sent in zip(calls, observed):
            assert call['request'] == sent['body']
            assert sent['authorization'] == f'Bearer {secret}?provider=azure'
            assert 'messages' not in sent['body'] and 'max_tokens' not in sent['body']
            assert 'max_completion_tokens' not in sent['body'] and 'tools' not in sent['body']
            assert sent['body']['store'] is False and sent['body']['stream'] is False
            assert sent['body']['text'] == {'format': {'type': 'json_object'}}
            assert sent['body']['reasoning'] == {'effort': 'low'}
        assert calls[-1]['usage']['cached_tokens'] == 10
        assert json.loads(calls[-1]['response']['raw']['text'])['status'] == 'completed'
        assert report(root)['latest_run_budget']['consumed']['model_requests'] == 8
        assert export_llm_calls(root, root / 'calls.jsonl')['physical_attempts'] == 8
        for role in ('test_case_synthesizer', 'reward_synthesizer'):
            output = root / (role + '.jsonl')
            exported = export_sft(root, output, actor_role=role)
            assert exported['samples'] == 1
            assert exported['tool_model_targets'] == {'harness_verifier': 0, 'rubric_judge': 0}
            sample = json.loads(output.read_text())
            actual = next(c for c in calls if c['actor_role'] == role and c['response_complete'])
            assert sample['prompt_messages'] == actual['request']['input']
            assert not any(sample['prompt_loss_mask'])
        before = (root / 'results.jsonl').read_bytes()
        assert replay(root)['new_requests'] == 0
        assert construct_file(None, None, root, resume=True) == []
        assert len(observed) == 8 and (root / 'results.jsonl').read_bytes() == before
        assert all(secret.encode() not in p.read_bytes() for p in root.rglob('*') if p.is_file())


@pytest.mark.parametrize('mutation', ['missing_output', 'failed', 'refusal', 'tool', 'missing_message_status', 'partial_message', 'filter', 'bad_incomplete'])
def test_responses_rejects_unusable_results(mutation):
    obj = response()
    if mutation == 'missing_output': obj.pop('output')
    elif mutation == 'failed': obj['status'] = 'failed'
    elif mutation == 'refusal': obj['output'][1]['content'] = [{'type': 'refusal', 'refusal': 'no'}]
    elif mutation == 'tool': obj['output'].append({'type': 'function_call', 'name': 'unexpected'})
    elif mutation == 'missing_message_status': obj['output'][1].pop('status')
    elif mutation == 'partial_message': obj['output'][1]['status'] = 'incomplete'
    elif mutation == 'filter': obj.update(status='incomplete', incomplete_details={'reason': 'content_filter'})
    elif mutation == 'bad_incomplete': obj.update(status='incomplete', incomplete_details='bad')
    adapter = HttpResponsesJsonV1(responses_model())
    try:
        with pytest.raises(ProviderError): adapter.parse_response(httpx.Response(200, json=obj))
    finally:
        adapter.close()


def test_responses_truncation_and_missing_usage_stay_explicit():
    obj = response('partial')
    obj.update(status='incomplete', incomplete_details={'reason': 'max_output_tokens'})
    obj['output'][1]['status'] = 'incomplete'
    obj.pop('usage')
    adapter = HttpResponsesJsonV1(responses_model())
    try:
        result = adapter.parse_response(httpx.Response(200, json=obj))
        assert result.finish_reason == 'length' and result.text == 'partial'
        assert not result.usage_known and result.prompt_tokens is None
    finally:
        adapter.close()


def test_responses_preflight_and_legacy_model_serialization(monkeypatch):
    legacy = ModelConfig(model='fixture').model_dump(mode='json')
    assert 'auth_provider' not in legacy and 'responses_store' not in legacy
    assert ModelConfig.model_validate(legacy).model_dump(mode='json') == legacy
    config = resolve_config(load_config(ROOT / 'configs/offline_v2.yaml'), ROOT / 'configs')
    ref = config.roles['rubric_judge']
    config.models[ref] = responses_model(api_key_env='RESPONSES_MISSING_KEY', auth_provider='azure')
    monkeypatch.delenv('RESPONSES_MISSING_KEY', raising=False)
    checked = preflight(config, ROOT)
    assert not checked.ok and any('RESPONSES_MISSING_KEY' in p for p in checked.problems)
    with pytest.raises(ValueError): ModelConfig(provider_adapter='http_responses_json_v1', model='gpt-4.1')
