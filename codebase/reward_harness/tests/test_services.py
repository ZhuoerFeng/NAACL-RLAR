from conftest import run_path
import contextlib
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import pytest
import httpx
from rlar_harness.driver import construct_file
from rlar_harness.llm.http_chat_json import HttpChatJsonAdapter
from rlar_harness.llm.scripted import ScriptedLLM
from rlar_harness.trace.export import read_run
from conftest import reward_calls as llm_calls
from rlar_harness.trace.report import report
from rlar_harness.runtime.runner import SubprocessRunner
from rlar_harness.budget import BudgetLedger, SharedBudget
from conftest import definition, turn

@contextlib.contextmanager
def server(responses):
    observed = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            observed.append({'body': body, 'authorization': self.headers.get('Authorization')})
            idx = min(len(observed) - 1, len(responses) - 1)
            spec = responses[idx]
            if spec is None:
                self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
            if callable(spec): spec = spec(body)
            status, obj = spec
            data = json.dumps(obj).encode()
            self.send_response(status); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length', str(len(data))); self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError): pass
        def log_message(self, *_): pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True); t.start()
    try: yield 'http://127.0.0.1:%d/chat' % httpd.server_port, observed
    finally: httpd.shutdown(); httpd.server_close(); t.join(timeout=2)


def response(text=None, finish='stop', usage=True):
    obj = {'id': 'mock-observable-id', 'choices': [{'message': {'role':'assistant','content': text or turn().content}, 'finish_reason':finish}]}
    if usage: obj['usage'] = {'prompt_tokens':40,'completion_tokens':20,'prompt_tokens_details':{'cached_tokens':10}}
    return obj


def http_config(config, url):
    c = config.model_copy(deep=True)
    c.role_model('reward_synthesizer').provider_adapter = 'http_chat_json_v1'; c.role_model('reward_synthesizer').endpoint = url
    c.role_model('reward_synthesizer').model = 'fixture-model'; c.role_model('reward_synthesizer').revision = 'fixture-revision'
    c.execution.allow_untrusted_code = True
    return c

def test_aihub_options_are_the_actual_wire_snapshot(config, source, tmp_path):
    with server([(200, response())]) as (url, requests):
        c = http_config(config, url)
        c.role_model('reward_synthesizer').max_tokens_field = 'max_completion_tokens'
        c.role_model('reward_synthesizer').reasoning_effort = 'low'
        c.role_model('reward_synthesizer').enable_thinking = True
        c.role_model('reward_synthesizer').top_p = 0.9
        c.role_model('reward_synthesizer').response_format = 'json_object'
        result = construct_file(c, source, run_path(tmp_path, 'TestRun'))[0]
        assert result.status == 'success'
        body = requests[0]['body']
        assert llm_calls(run_path(tmp_path, 'TestRun'))[0]['request'] == body
        assert body['max_completion_tokens'] == c.role_model('reward_synthesizer').max_output_tokens
        assert 'max_tokens' not in body
        assert body['reasoning_effort'] == 'low'
        assert body['enable_thinking'] is True
        assert body['top_p'] == 0.9
        assert body['response_format'] == {'type': 'json_object'}
        assert 'tools' not in body and 'stop' not in body

def test_gpt_quickstart_wire_fields_and_budget(config, source, tmp_path):
    with server([(200, response())]) as (url, requests):
        c = http_config(config, url)
        c.role_model('reward_synthesizer').model = 'gpt-6-luna'
        c.role_model('reward_synthesizer').max_tokens_field = 'max_completion_tokens'
        c.role_model('reward_synthesizer').reasoning_effort = 'low'
        c.role_model('reward_synthesizer').send_temperature = False
        c.role_model('reward_synthesizer').prompt_cache_key = 'harness-test-cache'
        result = construct_file(c, source, run_path(tmp_path, 'TestRun'))[0]
        assert result.status == 'success'
        body = requests[0]['body']
        assert set(body) == {'model', 'messages', 'max_completion_tokens',
                             'reasoning_effort', 'stream', 'prompt_cache_key'}
        assert body['prompt_cache_key'] == 'harness-test-cache'
        assert llm_calls(run_path(tmp_path, 'TestRun'))[0]['request'] == body

@pytest.mark.acceptance('AT-18', 'AT-32', 'AT-33')
def test_http_retry_wire_snapshots_adapter_fields_and_no_secrets(config, source, tmp_path, monkeypatch):
    with server([(429,{'error':'limited'}), (503,{'error':'temporary'}), (200,response())]) as (url, requests):
        c = http_config(config,url); c.role_model('reward_synthesizer').api_key_env = 'FIXTURE_CONTROLLER_KEY'
        monkeypatch.setenv('FIXTURE_CONTROLLER_KEY','secret-not-in-trace-8348')
        class Extended(HttpChatJsonAdapter):
            def build_body(self, request):
                return {**super().build_body(request), 'tools': [{'name':'fixture-schema'}],
                        'response_format': {'type':'json_object'}, 'adapter_added':'frozen-at-boundary',
                        'source':'adapter field with CRLF\r\nunchanged'}
        r = construct_file(c, source, run_path(tmp_path, 'TestRun'), llm_client=Extended(c.role_model('reward_synthesizer')))[0]
        assert r.status == 'success' and len(llm_calls(run_path(tmp_path, 'TestRun'))) == 3
        calls = llm_calls(run_path(tmp_path, 'TestRun'))
        assert len(requests) == len(calls) == 3
        assert len({c['request_digest'] for c in calls}) == 1
        for call, request in zip(calls, requests):
            assert call['request'] == request['body']
            assert request['authorization'] == 'Bearer secret-not-in-trace-8348'
        from rlar_harness.trace.export import export_llm_calls
        export_llm_calls(run_path(tmp_path, 'TestRun'),tmp_path/'wire.jsonl')
        expanded=[json.loads(line) for line in (tmp_path/'wire.jsonl').read_text().splitlines() if json.loads(line)['actor_role']=='reward_synthesizer']
        assert [c['request'] for c in expanded]==[r['body'] for r in requests]
        assert calls[-1]['response']['raw']['text'] == json.dumps(response())
        assert calls[-1]['usage']['cached_tokens'] == 10
        assert calls[-1]['committed_to_history']
        assert not calls[0]['committed_to_history']
        assert all(b'secret-not-in-trace-8348' not in p.read_bytes() for p in (run_path(tmp_path, 'TestRun')).rglob('*.blob'))
        stats = report(run_path(tmp_path, 'TestRun'))
        assert stats['transport_retries'] == 2 and stats['actor_usage']['reward_synthesizer']['physical_attempts'] == 3

@pytest.mark.acceptance('AT-19', 'AT-34')
def test_http_dispatched_unknown_is_replayed_and_charged(config, source, tmp_path):
    with server([None, (200,response())]) as (url, requests):
        c = http_config(config,url)
        r = construct_file(c, source, run_path(tmp_path, 'TestRun'))[0]
        calls = llm_calls(run_path(tmp_path, 'TestRun'))
        assert len(requests) == 2
        assert calls[0]['dispatch_status'] == 'unknown'
        assert calls[0]['request'] == calls[1]['request']
        assert r.usage.unknown_usage_events >= 1
        _, events, _, _ = read_run(run_path(tmp_path, 'TestRun'))
        ledger = [e.payload['episode'] for e in events if e.type == 'budget_snapshot'][-1]
        assert ledger['unknown_reservations']['model_requests'] == 1
        assert ledger['consumed']['model_requests'] == len(__import__('rlar_harness.trace.export',fromlist=['llm_calls']).llm_calls(run_path(tmp_path, 'TestRun')))

@pytest.mark.acceptance('AT-32')
@pytest.mark.parametrize('obj', [[], {}, {'choices':[]}, {'choices':[{}]}, {'choices':[{'message':{'content':None}}]}])
def test_http_bad_shapes_rejected(config, obj):
    with server([(200,obj)]) as (url, requests):
        adapter = HttpChatJsonAdapter(http_config(config,url).role_model('reward_synthesizer'))
        from rlar_harness.llm.adapter import ProviderError
        with pytest.raises(ProviderError): adapter.parse_response(httpx.Response(200,json=obj))
        adapter.close()

@pytest.mark.acceptance('AT-32', 'AT-17')
def test_http_finish_reason_and_unknown_usage(config, source, tmp_path):
    with server([(200,response(finish='length',usage=False)), (200,response())]) as (url, requests):
        r = construct_file(http_config(config,url), source, run_path(tmp_path, 'TestRun'))[0]
        calls = llm_calls(run_path(tmp_path, 'TestRun'))
        assert len(calls) == 2 and not calls[0]['response_complete']
        assert calls[0]['usage']['usage_known'] is False
        assert calls[1]['response_complete']
        assert r.usage.tool_calls == 4



@pytest.mark.acceptance('AT-27')
def test_auth_stops_outer_stream(config, source, tmp_path):
    source.write_text(source.read_text()+source.read_text().replace('q1','q2'))
    with server([(401, {'error':'auth'})]) as (url, requests):
        from rlar_harness.errors import HarnessError
        with pytest.raises(HarnessError, match='authentication'):
            construct_file(http_config(config,url), source, run_path(tmp_path, 'TestRun'))
        assert len(requests) == 1
        _, events, results, _ = read_run(run_path(tmp_path, 'TestRun'))
        assert results == []
        assert events[-1].type == 'run_interrupted'

@pytest.mark.acceptance('AT-22','AT-27')
def test_active_http_total_watchdog_is_bounded(config, source, tmp_path):
    def delayed(_):
        time.sleep(.6)
        return (200,response())
    with server([delayed]) as (url, requests):
        c=http_config(config,url); c.role_model('reward_synthesizer').total_timeout_s=.1; c.synthesis.model_transport_attempts=2
        from rlar_harness.errors import HarnessError
        started=time.monotonic()
        result=construct_file(c,source,run_path(tmp_path, 'TestRun'))
        assert result[0].stop_reason=='infrastructure_error'
        assert time.monotonic()-started < 1.5
        calls=llm_calls(run_path(tmp_path, 'TestRun'))
        assert len(calls)==2 and all(c['dispatch_status']=='unknown' for c in calls)

@pytest.mark.acceptance('AT-20')
def test_component_budget_admission_prevents_partial_suite(config, source, tmp_path):
    config.budget.episode['component_executions']=1
    root=run_path(tmp_path, 'TestRun')
    rows=construct_file(config,source,root,llm_client=ScriptedLLM([turn()]))
    assert rows[0].stop_reason=='budget_exhausted'
    assert rows[0].usage.component_executions==0
    assert not any(e.type=='score_batch_dispatched' for e in read_run(root)[1])

@pytest.mark.acceptance('AT-18','AT-27','AT-31')
def test_environment_circuit_breaker_preserves_unprocessed_records(config,source,tmp_path):
    line=source.read_text(); source.write_text(line+line.replace('q1','q2')+line.replace('q1','q3'))
    with server([(503,{'error':'offline'})]) as (url,requests):
        c=http_config(config,url); c.synthesis.model_transport_attempts=1; c.budget.retry.circuit_breaker_threshold=2
        root=run_path(tmp_path, 'TestRun'); result=construct_file(c,source,root)
        assert len(result)==2 and len(requests)==2
        assert all(r.stop_reason=='infrastructure_error' for r in result)
        stats=report(root)
        assert stats['counts']['input']==3 and stats['counts']['unprocessed']==1
        assert read_run(root)[1][-1].payload['reason']=='environment_circuit_breaker'
