from conftest import run_path
import json
from pathlib import Path
import pytest
from rlar_harness.driver import construct_file, RunDriver
from rlar_harness.llm.scripted import ScriptedLLM, Turn
from rlar_harness.trace.export import export_llm_calls, export_sft, read_run, replay
from rlar_harness.trace.report import report
from rlar_harness.storage.blobs import BlobStore
from rlar_harness.storage.journal import Journal
from rlar_harness.errors import StorageError, PreflightError
from rlar_harness.cli import audit_run, score_file
from conftest import definition, turn, ROOT, reward_calls as llm_calls, start_episode


def rows(path): return [json.loads(line) for line in Path(path).read_text().splitlines()]

@pytest.mark.acceptance('AT-29')
def test_formal_audit_refused_prototype_frozen_and_invisible(config, source, tmp_path):
    root=run_path(tmp_path, 'TestRun'); construct_file(config,source,root)
    before={name:(root/name).read_bytes() for name in ('trace.jsonl','results.jsonl','reward_library.jsonl')}
    suite=json.loads((ROOT/'tests/fixtures/math/suite_draft.json').read_text())
    for e in suite['examples']:e['query_ref']='q1'
    independent=tmp_path/'audit_input.json';independent.write_text(json.dumps({'q1':suite}))
    with pytest.raises(ValueError,match='isolated'):
        audit_run(root,independent,root/'audit.json',execution_run_dir=run_path(tmp_path, 'FormalAudit'))
    summary=audit_run(root,independent,root/'audit.json',allow_prototype=True,execution_run_dir=run_path(tmp_path, 'PrototypeAudit'))
    assert summary['reports'][0]['assurance']=='behavioral_prototype'
    assert summary['training_selection_affected'] is False
    assert all((root/name).read_bytes()==value for name,value in before.items())
    assert str(independent) not in json.dumps(llm_calls(root))

@pytest.mark.acceptance('AT-30','AT-35')
def test_replay_and_sft_causal_targets_no_reuse_or_automatic_targets(config, source, tmp_path):
    root=run_path(tmp_path, 'TestRun'); result=construct_file(config,source,root)
    before=(root/'trace.jsonl').read_bytes()
    replayed=replay(root)
    assert replayed['physical_attempts']==9
    manifest=export_sft(root,root/'per.jsonl')
    samples=rows(root/'per.jsonl')
    assert manifest['samples']==2
    assert samples[0]['episode_id']==samples[1]['episode_id']==result[0].episode_id
    assert samples[0]['split']==samples[1]['split']=='train'
    assert len(samples[0]['prompt_messages'])==2
    assert len(samples[1]['prompt_messages'])==4
    assert samples[1]['prompt_messages'][2]==samples[0]['target_message']
    assert 'false_accept_rate' in samples[1]['prompt_messages'][3]['content']
    assert all(s['loss_scope']=='target_assistant_only' and not any(s['prompt_loss_mask']) for s in samples)
    assert 'automatic_finalization' not in json.dumps(samples)
    export_sft(root,root/'full.jsonl',format='full_trace')
    full=rows(root/'full.jsonl')
    assert len(full)==1 and sum(full[0]['message_loss_mask'])==2
    export_sft(root,root/'program.jsonl',format='final_program_only')
    assert len(rows(root/'program.jsonl'))==1
    assert rows(root/'program.jsonl')[0]['target_origin']=='verified_artifact'
    assert rows(root/'program.jsonl')[0]['target_program']['components'][0]['source'].startswith('def score')
    assert before==(root/'trace.jsonl').read_bytes()

@pytest.mark.acceptance('AT-33')
def test_export_keeps_the_actual_bounded_observation(config, source, tmp_path):
    config.logging.max_observation_chars=400
    root=run_path(tmp_path, 'TestRun'); construct_file(config,source,root)
    export_llm_calls(root,root/'calls.jsonl')
    calls=[c for c in rows(root/'calls.jsonl') if c['actor_role']=='reward_synthesizer']
    text=calls[1]['request']['messages'][-1]['content']
    assert len(text)<=400 and '[truncated]' in text
    assert calls[1]['request']==llm_calls(root)[1]['request']
    assert len(json.dumps(read_run(root)[0].get_json(next(e.payload['validation_ref'] for e in read_run(root)[1] if e.type=='evaluation_result'))))>len(text)

@pytest.mark.acceptance('AT-34','AT-35')
def test_invalid_truncated_retry_returns_excluded_but_history_retained(config, source, tmp_path):
    root=run_path(tmp_path, 'TestRun')
    bad=Turn('{broken json')
    truncated=turn(); truncated.finish_reason='length'
    final=turn(); final.faults=['read_timeout_after_dispatch']
    construct_file(config,source,root,llm_client=ScriptedLLM([bad,truncated,final]))
    calls=llm_calls(root)
    assert len(calls)==4
    assert calls[0]['response_complete'] and calls[0]['committed_to_history'] and not calls[0]['schema_valid']
    assert not calls[1]['response_complete']
    assert calls[2]['dispatch_status']=='unknown'
    exported=export_sft(root,root/'sft.jsonl')
    samples=rows(root/'sft.jsonl')
    assert len(samples)==1
    assert '{broken json' in json.dumps(samples[0]['prompt_messages'])
    assert not any(samples[0]['prompt_loss_mask'])
    assert exported['excluded']['incomplete_invalid_or_uncommitted']==3

@pytest.mark.acceptance('AT-34')
def test_late_return_archived_without_target_or_actions(config, source, tmp_path):
    from rlar_harness.inputs import iter_jsonl
    from rlar_harness.llm.history import EpisodeHistory
    from rlar_harness.schemas import Message
    with RunDriver(run_path(tmp_path, 'TestRun'),config=config,input_path=source,llm_client=ScriptedLLM([turn()])) as driver:
        item=next(iter_jsonl(source)); s,budget,dispatcher,llm=start_episode(driver,item,config)
        history=EpisodeHistory((Message.model_validate(s['history'][0]),),(Message.model_validate(s['history'][1]),))
        logical=s['episode_id']+':call:1'
        result=llm.call(history,logical_call_id=logical)
        history.append_observation('new cursor')
        assert not llm.accept(history,result,logical,schema_valid=True)
        calls=llm_calls(run_path(tmp_path, 'TestRun'))
        assert len(calls)==1 and calls[0]['response_complete']
        assert not calls[0]['committed_to_history'] and not calls[0]['actions_dispatched']
        assert export_sft(run_path(tmp_path, 'TestRun'),tmp_path/'sft.jsonl')['samples']==0

@pytest.mark.acceptance('AT-36')
@pytest.mark.parametrize('damage', ['missing','hash'])
def test_export_fails_on_corruption_without_reconstruction(config, source, tmp_path, damage):
    root=run_path(tmp_path, 'TestRun'); construct_file(config,source,root)
    calls=llm_calls(root); store=BlobStore(root/'blobs')
    # Content addressing deduplicates exact complete bodies without lossy prompts.
    body=calls[0]['request']; ref=store.put_json(body)
    assert ref==calls[0]['request_body_ref']
    path=store.ref_to_path(ref)
    if damage=='missing': path.unlink()
    else: path.write_bytes(b'{}')
    with pytest.raises(StorageError): export_llm_calls(root,root/'export.jsonl')
    with pytest.raises(StorageError): export_sft(root,root/'export-sft.jsonl')

@pytest.mark.acceptance('AT-36','AT-35')
def test_legacy_unavailable_and_window_exclusions_are_explicit(config, source, tmp_path):
    root=run_path(tmp_path, 'TestRun'); construct_file(config,source,root)
    blobs=BlobStore(root/'blobs'); journal=Journal(root/'trace.jsonl',blobs); journal.scan()
    journal.append('llm_call_completed',{'logical_call_id':'legacy-without-request'})
    m=export_sft(root,root/'sft.jsonl',max_tokens=1)
    assert m['samples']==0
    assert m['excluded']['legacy_trace_unavailable']==1
    assert m['excluded']['context_window']==2

@pytest.mark.acceptance('AT-31')
def test_report_keeps_failure_partial_retry_and_unknown_denominators(config, source, tmp_path):
    config.construction.reuse_enabled=False; config.budget.episode['controller_steps']=3
    source.write_text(source.read_text()+source.read_text().replace('q1','q2'))
    partial=definition(sources=['def score(e,c): return numeric_score(e, None)','def score(e,c): return 1/0'])
    first=turn(partial); first.faults=['read_timeout_after_dispatch']
    root=run_path(tmp_path, 'TestRun'); construct_file(config,source,root,llm_client=ScriptedLLM([first,turn(),turn(definition('1.0')),turn(definition('1.0'))]))
    r=report(root)
    assert r['counts']['input']==2 and r['counts']['success']==r['counts']['failed']==1
    assert r['partial_cases']==2
    assert r['controller_logical_calls']==0 and r['actor_usage']['reward_synthesizer']['logical_calls']==4 and r['actor_usage']['reward_synthesizer']['physical_attempts']==5
    assert r['transport_retries']==1 and r['unknown_usage_events']==1
    assert r['cost_usd'] is None and r['cost_status']=='unknown'
    assert len(r['episodes'])==2 and r['mask_distribution']

@pytest.mark.acceptance('AT-02','AT-04')
def test_score_cli_uses_frozen_artifact_for_all_candidates(config, source, tmp_path):
    root=run_path(tmp_path, 'TestRun'); result=construct_file(config,source,root)
    source_candidates=tmp_path/'candidates.jsonl'
    source_candidates.write_text('\n'.join(json.dumps({'query_id':'q1','response':answer,'group_id':'g'}) for answer in ['\\boxed{5/6}','\\boxed{7}'])+'\n')
    score_file(root,source_candidates,root/'scores.jsonl')
    scored=rows(root/'scores.jsonl')
    assert [s['total_score'] for s in scored]==[1,0]
    assert {s['reward_key'] for s in scored}=={result[0].reward_key}
