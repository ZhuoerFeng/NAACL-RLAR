from conftest import run_path
import json
import time
from pathlib import Path
import pytest
from rlar_harness.driver import construct_file, RunDriver
from rlar_harness.llm.scripted import ScriptedLLM, Turn
from rlar_harness.trace.export import replay
from conftest import reward_calls as llm_calls, start_episode
from rlar_harness.inputs import iter_jsonl
from rlar_harness.schemas import LLMResult, Message
from conftest import definition, turn

@pytest.mark.acceptance('AT-11', 'AT-12')
def test_zero_one_two_decision_paths_and_incompatible_mode(config, source, tmp_path):
    source.write_text(source.read_text() + source.read_text().replace('q1', 'q2'))
    rows = construct_file(config, source, run_path(tmp_path, 'TestRun'))
    assert [sum(c['episode_id']==r.episode_id for c in llm_calls(run_path(tmp_path, 'TestRun'))) for r in rows] == [2, 2]
    assert [r.reused for r in rows] == [False, False]
    source2 = tmp_path/'one.jsonl'; source2.write_text(source.read_text().splitlines()[0]+'\n')
    first = construct_file(config, source2, run_path(tmp_path, 'FirstConstruction'), llm_client=ScriptedLLM([turn()]))[0]
    assert len(llm_calls(run_path(tmp_path, 'FirstConstruction'))) == 1
    with RunDriver(run_path(tmp_path, 'TestRun'), resume=True) as driver:
        pack = driver.packs.get('math_v2')
        decision = driver.library.compatible_entries(driver.library.snapshot(), pack, 'single')
        assert decision == ()
        pack.applicability_rule.task_contract_digest = 'incompatible-same-text-tag'
        decision = driver.library.compatible_entries(driver.library.snapshot(), pack, 'checklist')
        assert decision == ()

@pytest.mark.acceptance('AT-13')
def test_single_completion_never_repairs(config, source, tmp_path):
    config.construction.strategy = 'single_completion'
    adapter = ScriptedLLM([turn(definition('1.0')), turn()])
    r = construct_file(config, source, run_path(tmp_path, 'TestRun'), llm_client=adapter)[0]
    assert r.status == 'failed' and len(llm_calls(run_path(tmp_path, 'TestRun'))) == 1
    assert adapter.turns_consumed == 1
    from rlar_harness.trace.export import read_run
    blobs, events, _, _ = read_run(run_path(tmp_path, 'TestRun'))
    report = blobs.get_json(next(e.payload['validation_ref'] for e in events if e.type == 'evaluation_result'))
    assert any(d['passed'] is False for d in report['decisions'])

@pytest.mark.acceptance('AT-14')
def test_three_decisions_preserve_every_prefix_message(config, source, tmp_path):
    adapter = ScriptedLLM([turn(definition('1.0')), turn(definition('0.0')), turn()])
    r = construct_file(config, source, run_path(tmp_path, 'TestRun'), llm_client=adapter)[0]
    assert len(llm_calls(run_path(tmp_path, 'TestRun'))) == 3 and r.status == 'success'
    calls = llm_calls(run_path(tmp_path, 'TestRun'))
    for a, b in zip(calls, calls[1:]):
        before = a['request']['messages']; after = b['request']['messages']
        assert after[:len(before)] == before
        assert a['request']['messages'][0] == b['request']['messages'][0]
        assert b['expected_history_cursor'] > a['expected_history_cursor']
    assert replay(run_path(tmp_path, 'TestRun'))['new_requests'] == 0

@pytest.mark.acceptance('AT-15')
def test_query_histories_are_independent(config, source, tmp_path):
    config.construction.reuse_enabled = False
    source.write_text(source.read_text() + source.read_text().replace('q1','q2').replace('1/2 + 1/3', '9/10 + 1/10'))
    construct_file(config, source, run_path(tmp_path, 'TestRun'), llm_client=ScriptedLLM([turn(), turn()]))
    calls = llm_calls(run_path(tmp_path, 'TestRun'))
    assert len(calls) == 2
    assert calls[0]['request']['messages'][0] == calls[1]['request']['messages'][0]
    assert len(calls[1]['request']['messages']) == 2
    assert 'q1' not in json.dumps(calls[1]['request']['messages'])

@pytest.mark.acceptance('AT-16')
def test_parallel_read_barrier_and_stale_cursor(config, source, tmp_path):
    reads = Turn.actions({'id':'slow', 'tool':'read_resource', 'arguments':{'resource_id':'slow'}},
                         {'id':'fast', 'tool':'read_resource', 'arguments':{'resource_id':'fast'}})
    adapter = ScriptedLLM([reads, turn()])
    with RunDriver(run_path(tmp_path, 'TestRun'), config=config, input_path=source, llm_client=adapter) as driver:
        from rlar_harness.tools.resources import Resource
        from rlar_harness.episode import EpisodeContext, construct_one
        item = next(iter_jsonl(source)); pack = driver.packs.get('math_v2')
        s, budget, dispatcher, llm = start_episode(driver,item,config)
        dispatcher.resources.add(Resource('slow','text','slow', lambda: (time.sleep(.15), 'slow-value')[1]))
        dispatcher.resources.add(Resource('fast','text','fast', lambda: 'fast-value'))
        c = EpisodeContext(s, config, dispatcher, llm, budget, driver.journal, driver.blobs, driver.root, driver.deadline)
        final = construct_one(item.record, c)
        driver._commit(item, final, budget, dispatcher, pack)
        events = driver.journal.scan().events
        results = [e.payload['action_id'] for e in events if e.type == 'tool_result']
        assert results[0].endswith(':fast') and results[1].endswith(':slow')
        body = adapter.requests[1].messages[-1].content
        assert body.index('slow-value') < body.index('fast-value')
        from rlar_harness.llm.history import EpisodeHistory
        history = EpisodeHistory.restore((Message.model_validate(s['history'][0]),), (Message.model_validate(s['history'][1]),), [])
        before = history.cursor
        assert not llm.accept(history, LLMResult(status='complete', history_cursor=99, trace_ref='old'), 'late', schema_valid=True)
        assert history.cursor == before

@pytest.mark.acceptance('AT-17')
@pytest.mark.parametrize('bad', ['json', 'unknown', 'arguments', 'truncated'])
def test_invalid_batches_and_truncation_never_execute(config, source, tmp_path, bad):
    if bad == 'json': first = Turn('{not json')
    elif bad == 'unknown': first = Turn.actions({'id':'x','tool':'delete_everything','arguments':{}})
    elif bad == 'arguments': first = Turn.actions({'id':'x','tool':'test_reward','arguments':{'PASS':True}})
    else: first = turn(); first.finish_reason = 'length'
    rows = construct_file(config, source, run_path(tmp_path, 'TestRun'), llm_client=ScriptedLLM([first, turn()]))
    assert rows[0].status == 'success'
    assert len(llm_calls(run_path(tmp_path, 'TestRun'))) == 2
    assert rows[0].usage.tool_calls == 4  # one reward test plus three verifier decisions
    calls = llm_calls(run_path(tmp_path, 'TestRun'))
    assert not calls[0]['actions_dispatched']
    if bad == 'truncated': assert not calls[0]['committed_to_history']

@pytest.mark.acceptance('AT-20', 'AT-22')
@pytest.mark.parametrize('limit', ['context', 'steps', 'revision', 'stagnation', 'deadline', 'tokens'])
def test_finite_termination(config, source, tmp_path, limit):
    if limit == 'context': config.role_model('reward_synthesizer').context_limit_tokens = 10
    elif limit == 'steps': config.budget.episode['controller_steps'] = 1
    elif limit == 'revision': config.budget.episode['revisions'] = 0
    elif limit == 'stagnation': config.budget.no_progress_threshold = 2
    elif limit == 'deadline': config.budget.absolute_deadline_utc = time.time() - 1
    else: config.budget.episode['input_tokens'] = 1
    adapter = ScriptedLLM([turn(definition('1.0')) for _ in range(6)])
    rows = construct_file(config, source, run_path(tmp_path, 'TestRun'), llm_client=adapter)
    assert rows[0].status == 'failed'
    assert rows[0].stop_reason in ('context_budget_exhausted','budget_exhausted','deadline_exceeded','quality_not_met')
    assert adapter.physical_attempts <= 2

@pytest.mark.acceptance('AT-22')
def test_budget_stop_selects_previous_eligible(config, source, tmp_path):
    config.budget.episode['controller_steps'] = 2
    rows = construct_file(config, source, run_path(tmp_path, 'TestRun'), llm_client=ScriptedLLM([turn(on_pass='return')]))
    assert rows[0].status == 'success' and rows[0].stop_reason == 'budget_exhausted'

@pytest.mark.acceptance('AT-08','AT-12')
def test_explicit_submit_uses_same_finalizer(config, source, tmp_path):
    def submit(request):
        observation=json.loads(request.messages[-1].content)
        r=observation['results'][0]['result']
        return Turn.actions({'id':'submit','tool':'submit_reward','arguments':{
            'reward_key':r['reward_key'],'validation_run_id':r['validation_run_id']}})
    adapter=ScriptedLLM([turn(on_pass='return')],on_exhausted=submit)
    rows=construct_file(config,source,run_path(tmp_path, 'TestRun'),llm_client=adapter)
    assert rows[0].status=='success' and len(llm_calls(run_path(tmp_path, 'TestRun')))==2

@pytest.mark.acceptance('AT-16')
def test_multi_candidate_selector_is_metric_then_key(config, source, tmp_path):
    a=definition(); b=definition('float(numeric_score(example, None))')
    t=Turn.actions(json.loads(turn(a).content)['actions'][0],
        {**json.loads(turn(b).content)['actions'][0],'id':'second'})
    rows=construct_file(config,source,run_path(tmp_path, 'TestRun'),llm_client=ScriptedLLM([t]))
    from rlar_harness.storage.canonical import reward_key_for
    assert rows[0].reward_key==min(reward_key_for(a),reward_key_for(b))
    assert len(llm_calls(run_path(tmp_path, 'TestRun')))==1
    assert rows[0].usage.test_cases==6

@pytest.mark.acceptance('AT-20','AT-22')
def test_global_budget_stops_taking_new_queries(config,source,tmp_path):
    config.budget.run['controller_steps']=2
    source.write_text(source.read_text()+source.read_text().replace('q1','q2'))
    root=run_path(tmp_path, 'TestRun')
    result=construct_file(config,source,root,llm_client=ScriptedLLM([turn(),turn()]))
    assert len(result)==1 and result[0].status=='success'
    from rlar_harness.trace.export import read_run
    assert read_run(root)[1][-1].payload['reason']=='global_budget_exhausted'
    assert len(llm_calls(root))==1
    before=(root/'results.jsonl').read_bytes()
    assert construct_file(None,None,root,resume=True)==[]
    assert (root/'results.jsonl').read_bytes()==before
