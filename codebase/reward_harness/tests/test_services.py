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
from rlar_harness.llm.http_chat_json_v1 import HttpChatJsonV1
from rlar_harness.llm.scripted import ScriptedLLM
from rlar_harness.trace.export import llm_calls, read_run
from rlar_harness.trace.report import report
from rlar_harness.runtime.broker import ScoringBroker
from rlar_harness.runtime.runner import SubprocessRunner
from rlar_harness.budget import BudgetLedger, SharedBudget
from rlar_harness.config import RMConfig, RMCatalogueEntry
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
    c.model.provider_adapter = 'http_chat_json_v1'; c.model.endpoint = url
    c.model.model = 'fixture-model'; c.model.revision = 'fixture-revision'
    c.execution.allow_untrusted_code = True
    return c

@pytest.mark.acceptance('AT-18', 'AT-32', 'AT-33')
def test_http_retry_wire_snapshots_adapter_fields_and_no_secrets(config, source, tmp_path, monkeypatch):
    with server([(429,{'error':'limited'}), (503,{'error':'temporary'}), (200,response())]) as (url, requests):
        c = http_config(config,url); c.model.api_key_env = 'FIXTURE_CONTROLLER_KEY'
        monkeypatch.setenv('FIXTURE_CONTROLLER_KEY','secret-not-in-trace-8348')
        class Extended(HttpChatJsonV1):
            def build_body(self, request):
                return {**super().build_body(request), 'tools': [{'name':'fixture-schema'}],
                        'response_format': {'type':'json_object'}, 'adapter_added':'frozen-at-boundary'}
        r = construct_file(c, source, tmp_path/'run', llm_client=Extended(c.model))[0]
        assert r.status == 'success' and r.usage.controller_logical_calls == 1
        calls = llm_calls(tmp_path/'run')
        assert len(requests) == len(calls) == 3
        assert len({c['request_digest'] for c in calls}) == 1
        for call, request in zip(calls, requests):
            assert call['request'] == request['body']
            assert request['authorization'] == 'Bearer secret-not-in-trace-8348'
        assert calls[-1]['response']['raw']['text'] == json.dumps(response())
        assert calls[-1]['usage']['cached_tokens'] == 10
        assert calls[-1]['committed_to_history']
        assert not calls[0]['committed_to_history']
        assert all(b'secret-not-in-trace-8348' not in p.read_bytes() for p in (tmp_path/'run').rglob('*.blob'))
        stats = report(tmp_path/'run')
        assert stats['transport_retries'] == 2 and stats['physical_requests'] == 3

@pytest.mark.acceptance('AT-19', 'AT-34')
def test_http_dispatched_unknown_is_replayed_and_charged(config, source, tmp_path):
    with server([None, (200,response())]) as (url, requests):
        c = http_config(config,url)
        r = construct_file(c, source, tmp_path/'run')[0]
        calls = llm_calls(tmp_path/'run')
        assert len(requests) == 2
        assert calls[0]['dispatch_status'] == 'unknown'
        assert calls[0]['request'] == calls[1]['request']
        assert r.usage.unknown_usage_events >= 1
        _, events, _, _ = read_run(tmp_path/'run')
        ledger = [e.payload['episode'] for e in events if e.type == 'budget_snapshot'][-1]
        assert ledger['unknown_reservations']['model_requests'] == 1
        assert ledger['consumed']['model_requests'] == 2

@pytest.mark.acceptance('AT-32')
@pytest.mark.parametrize('obj', [[], {}, {'choices':[]}, {'choices':[{}]}, {'choices':[{'message':{'content':None}}]}])
def test_http_bad_shapes_rejected(config, obj):
    with server([(200,obj)]) as (url, requests):
        adapter = HttpChatJsonV1(http_config(config,url).model)
        from rlar_harness.llm.adapter import ProviderError
        with pytest.raises(ProviderError): adapter.parse_response(httpx.Response(200,json=obj))
        adapter.close()

@pytest.mark.acceptance('AT-32', 'AT-17')
def test_http_finish_reason_and_unknown_usage(config, source, tmp_path):
    with server([(200,response(finish='length',usage=False)), (200,response())]) as (url, requests):
        r = construct_file(http_config(config,url), source, tmp_path/'run')[0]
        calls = llm_calls(tmp_path/'run')
        assert len(calls) == 2 and not calls[0]['response_complete']
        assert calls[0]['usage']['usage_known'] is False
        assert calls[1]['response_complete']
        assert r.usage.tool_calls == 1

@pytest.mark.acceptance('AT-20', 'AT-28')
def test_rm_broker_physical_retry_shared_budget_and_worker_key_boundary(config, tmp_path, monkeypatch):
    with server([(503, {'error':'retry'}), (200, {'score': .75, 'usage': {'prompt_tokens':3,'completion_tokens':1}})]) as (url, requests):
        entry = RMCatalogueEntry(model_id='rm', revision='r1', model_card_ref='fixture-card', supported_inputs=['query','response'],
            score_semantics='raw scalar', normalization_id='identity', endpoint_ref=url, api_key_env='FIXTURE_RM_KEY')
        monkeypatch.setenv('FIXTURE_RM_KEY', 'never-in-worker-663')
        rm = RMConfig(enabled=True, catalogue=[entry])
        ledger = SharedBudget(BudgetLedger('run', {'model_requests':2, 'scoring_requests':2, 'input_tokens':10000,'output_tokens':1000}),
                              BudgetLedger('episode', {'model_requests':2, 'scoring_requests':2}))
        events = []
        broker = ScoringBroker(rm, budget=ledger, on_event=lambda k,v: events.append((k,v)))
        payload = {'kind':'score_model','model_id':'rm','query':'q','response':'a'}
        assert broker.handle_scoring_request({**payload,'model_id':'unlisted'})['code'] == 'model_not_in_catalogue'
        assert broker.handle_scoring_request(payload)['score'] == .75
        assert len(requests) == 2 and requests[0]['body'] == requests[1]['body']
        assert requests[0]['authorization'] == 'Bearer never-in-worker-663'
        assert broker.handle_scoring_request(payload)['code'] == 'scoring_budget_exhausted'
        assert len(requests) == 2
        assert ledger.run.consumed['model_requests'] == 2
        assert ledger.episode.consumed['scoring_requests'] == 2
        assert 'never-in-worker' not in json.dumps(events)
        from rlar_harness.cli import score_reward
        from rlar_harness.evaluation.taskpack import TaskPackStore
        pack = TaskPackStore(Path(config.data.task_pack_root)).get('math_v1')
        d = definition(sources=['import os\ndef score(e,c): return float(os.getenv("FIXTURE_RM_KEY") is None)'], api=False)
        assert score_reward(d, {}, pack, SubprocessRunner()).total_score == 1

@pytest.mark.acceptance('AT-28')
def test_rm_actual_worker_ipc(config):
    with server([(200, {'score':.5})]) as (url, requests):
        entry = RMCatalogueEntry(model_id='rm', revision='r1', model_card_ref='card', supported_inputs=['query','response'],
            score_semantics='scalar', normalization_id='identity', endpoint_ref=url)
        broker = ScoringBroker(RMConfig(enabled=True, catalogue=[entry]))
        from rlar_harness.cli import score_reward
        from rlar_harness.evaluation.taskpack import TaskPackStore
        from rlar_harness.schemas import RewardDefinition
        pack = TaskPackStore(Path(config.data.task_pack_root)).get('math_v1'); pack.permitted_apis = ['reward_model_scoring_v1']
        raw = definition(sources=['def score(e,c): return c.score_model("rm", e["query"], e["response"])']).model_dump(mode='json')
        raw['components'][0]['required_apis'] = ['reward_model_scoring_v1']
        result = score_reward(RewardDefinition.model_validate(raw), {'query':'q','response':'a'}, pack, SubprocessRunner(scoring_service=broker))
        assert result.total_score == .5 and len(requests) == 1
        assert broker.usage.scoring_requests == 1

@pytest.mark.acceptance('AT-27')
def test_auth_stops_outer_stream(config, source, tmp_path):
    source.write_text(source.read_text()+source.read_text().replace('q1','q2'))
    with server([(401, {'error':'auth'})]) as (url, requests):
        from rlar_harness.errors import HarnessError
        with pytest.raises(HarnessError, match='authentication'):
            construct_file(http_config(config,url), source, tmp_path/'run')
        assert len(requests) == 1
        _, events, results, _ = read_run(tmp_path/'run')
        assert results == []
        assert events[-1].type == 'run_interrupted'

@pytest.mark.acceptance('AT-22','AT-27')
def test_active_http_total_watchdog_is_bounded(config, source, tmp_path):
    def delayed(_):
        time.sleep(.6)
        return (200,response())
    with server([delayed]) as (url, requests):
        c=http_config(config,url); c.model.total_timeout_s=.1; c.budget.retry.max_transport_attempts=2
        from rlar_harness.errors import HarnessError
        started=time.monotonic()
        with pytest.raises(HarnessError): construct_file(c,source,tmp_path/'run')
        assert time.monotonic()-started < 1.5
        calls=llm_calls(tmp_path/'run')
        assert len(calls)==2 and all(c['dispatch_status']=='unknown' for c in calls)

@pytest.mark.acceptance('AT-20')
def test_component_budget_admission_prevents_partial_suite(config, source, tmp_path):
    config.budget.episode['component_executions']=5
    root=tmp_path/'run'
    rows=construct_file(config,source,root,llm_client=ScriptedLLM([turn()]))
    assert rows[0].stop_reason=='budget_exhausted'
    assert rows[0].usage.component_executions==0
    assert not any(e.type=='evaluation_dispatched' for e in read_run(root)[1])
