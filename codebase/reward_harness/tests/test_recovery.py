from __future__ import annotations
from conftest import run_path
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
import pytest
from rlar_harness.driver import RunDriver, construct_file
from rlar_harness.errors import StorageError, HarnessError, RunDirLockedError
from rlar_harness.storage.blobs import BlobStore
from rlar_harness.storage.journal import Journal
from rlar_harness.storage.lock import RunDirLock
from rlar_harness.trace.export import llm_calls, replay, read_run, export_sft
from rlar_harness.llm.scripted import ScriptedLLM
from conftest import definition, turn


def cli(*args, env=None, timeout=30):
    return subprocess.run([sys.executable, '-m', 'rlar_harness', *map(str,args)], env=env, capture_output=True, text=True, timeout=timeout)


def write_config(config, tmp_path):
    import yaml
    path = tmp_path/'config.yaml'; path.write_text(yaml.safe_dump(config.model_dump(mode='json')))
    return path

@pytest.mark.acceptance('AT-23', 'AT-24', 'AT-25', 'AT-20')
@pytest.mark.parametrize('point', ['llm_before_dispatch','llm_after_dispatch','llm_after_response',
    'tool_before_dispatch','tool_after_dispatch','tool_after_result', 'after_library_before_result','after_result_before_checkpoint'])
def test_sigkill_recovery_is_idempotent(config, source, tmp_path, point):
    root = run_path(tmp_path, 'TestRun'); cfg = write_config(config,tmp_path)
    source.write_text(source.read_text()+source.read_text().replace('q1','q2'))
    env = {**os.environ, 'RLAR_TEST_KILL_AT':point}
    killed = cli('construct','--config',cfg,'--input',source,'--run-dir',root,env=env)
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    blobs, before_events, before_results, _ = read_run(root)
    before_responses = sum(e.type == 'llm_response' for e in before_events)
    before_evaluations = sum(e.type == 'evaluation_result' for e in before_events)
    if point == 'after_library_before_result':
        with RunDriver(root, resume=True) as driver:
            assert len(driver.library) == 0
            assert len(driver.library.unpublished) >= 1
            assert driver.results.results == []
    resumed = cli('resume','--run-dir',root)
    assert resumed.returncode == 0, resumed.stderr
    blobs, events, results, _ = read_run(root)
    assert len(results) == 2 and all(r.status == 'success' for r in results)
    assert not results[1].reused  # query-bound frozen suite differs
    assert results[0].usage.controller_logical_calls == 0
    calls = llm_calls(root)
    assert len(calls) == (19 if point == 'llm_after_dispatch' else 18)
    assert sum(c['committed_to_history'] for c in calls) == 18
    if point == 'llm_after_dispatch':
        assert calls[0]['dispatch_status'] == 'unknown'
        assert results[0].usage.unknown_usage_events >= 1
    if point == 'llm_after_response':
        assert sum(e.type == 'llm_response' for e in events) == 18
    if point == 'tool_after_result':
        assert sum(e.type == 'evaluation_result' for e in events) == 4
    snapshots = [blobs.get_json(e.payload['state_ref'])['library_snapshot'] for e in events
                 if e.type == 'episode_saved' and e.episode_id == results[0].episode_id]
    assert len(set(snapshots)) == 1
    state = replay(root)
    assert state['new_requests'] == 0
    assert sum(m['actor']=='assistant' for m in state['episodes'][results[0].episode_id]['messages']) == 2
    before = (root/'results.jsonl').read_bytes()
    again = cli('resume','--run-dir',root)
    assert again.returncode == 0 and '"new_results": 0' in again.stdout
    assert before == (root/'results.jsonl').read_bytes()
    exported = export_sft(root, root/'sft.jsonl')
    assert exported['samples'] == 4

@pytest.mark.acceptance('AT-26')
def test_torn_tails_corruption_missing_blob_and_double_writer(config, source, tmp_path):
    root = run_path(tmp_path, 'TestRun')
    construct_file(config,source,root)
    original = (root/'results.jsonl').read_bytes()
    for name in ('trace.jsonl','results.jsonl','reward_library.jsonl'):
        with (root/name).open('ab') as f: f.write(b'{"torn":')
    with RunDriver(root,resume=True): pass
    assert (root/'results.jsonl').read_bytes() == original
    with RunDirLock(root):
        second = cli('resume','--run-dir',root)
        assert second.returncode == 2 and 'live writer' in second.stderr
    raw = (root/'trace.jsonl').read_bytes()
    (root/'trace.jsonl').write_bytes(b'{bad}\n'+raw)
    with pytest.raises(StorageError): RunDriver(root,resume=True)
    (root/'trace.jsonl').write_bytes(raw)
    calls = llm_calls(root)
    blob = BlobStore(root/'blobs').ref_to_path(calls[0]['request_body_ref'])
    blob.unlink()
    with pytest.raises(StorageError): RunDriver(root,resume=True)

@pytest.mark.acceptance('AT-27')
def test_disk_failure_before_dispatch_does_not_spend_request(config, source, tmp_path, monkeypatch):
    adapter = ScriptedLLM([turn()])
    original = Journal.append
    def failed(self, kind, *args, **kwargs):
        if kind == 'llm_prepared': raise OSError('injected disk full')
        return original(self,kind,*args,**kwargs)
    monkeypatch.setattr(Journal,'append',failed)
    with pytest.raises(OSError,match='disk full'):
        construct_file(config,source,run_path(tmp_path, 'TestRun'),llm_client=adapter)
    assert adapter.physical_attempts == 0
    assert not (run_path(tmp_path)/'results.jsonl').exists()

@pytest.mark.acceptance('AT-27')
def test_sigterm_cancellation_never_commits_or_restarts(config, source, tmp_path):
    # Known fixture reward hangs. Cancel the actual driver while its worker runs.
    marker = tmp_path/'worker-started'
    d = definition(sources=['def score(e,c):\n while True: pass\n'],api=False)
    script = tmp_path/'script.json'; script.write_text(json.dumps({'math_v2':[{'content':turn(d).content}]}))
    config.role_model('reward_synthesizer').scripted_responses = str(script)
    config.execution.wall_timeout_s = 20; config.execution.cpu_timeout_s = 20
    cfg = write_config(config,tmp_path); root=run_path(tmp_path, 'TestRun')
    proc = subprocess.Popen([sys.executable,'-m','rlar_harness','construct','--config',str(cfg),'--input',str(source),'--run-dir',str(root)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    deadline = time.monotonic()+10
    while not worker_pid(proc.pid) and time.monotonic()<deadline: time.sleep(.02)
    assert worker_pid(proc.pid)
    proc.send_signal(signal.SIGTERM)
    out,err = proc.communicate(timeout=5)
    assert proc.returncode == 130, err
    _,events,results,_=read_run(root)
    assert results == [] and events[-1].type == 'run_interrupted'
    count = len(events); time.sleep(.05)
    assert len(read_run(root)[1]) == count

@pytest.mark.acceptance('AT-22', 'AT-23')
def test_restart_keeps_expired_deadline_and_budget(config, source, tmp_path):
    config.budget.wall_deadline_s = .5
    cfg=write_config(config,tmp_path); root=run_path(tmp_path, 'TestRun')
    killed=cli('construct','--config',cfg,'--input',source,'--run-dir',root,env={**os.environ,'RLAR_TEST_KILL_AT':'llm_after_dispatch'})
    assert killed.returncode == -signal.SIGKILL
    time.sleep(.6)
    resumed=cli('resume','--run-dir',root)
    assert resumed.returncode == 0,resumed.stderr
    _,_,rows,_=read_run(root)
    assert rows[0].status == 'failed' and rows[0].stop_reason == 'budget_exhausted'
    assert rows[0].usage.physical_requests == 1

@pytest.mark.acceptance('AT-21')
@pytest.mark.parametrize('kind', ['loop','stdout','child'])
def test_worker_resource_bounds_and_process_group_cleanup(config, tmp_path, kind):
    from rlar_harness.runtime.runner import SubprocessRunner, make_execute_request
    from rlar_harness.runtime.aggregate import aggregate
    from rlar_harness.storage.canonical import reward_key_for
    marker=tmp_path/'child.pid'
    if kind == 'loop': source='def score(e,c):\n while True: pass\n'
    elif kind == 'stdout': source='def score(e,c):\n print("x"*10000000, flush=True)\n return 1.0\n'
    else: source=f'import subprocess,sys\nfrom pathlib import Path\ndef score(e,c):\n p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n Path({str(marker)!r}).write_text(str(p.pid))\n return 1.0\n'
    d=definition(sources=[source,'def score(e,c): return 0.0'],api=False)
    request=make_execute_request(definition=d,examples=[{}],example_ids=['x'],permitted_apis=[],runtime_fingerprint='python-stdlib-fixture-v1',wall_timeout_s=.3,cpu_timeout_s=1,memory_limit_mb=None,max_output_bytes=1024,max_return_bytes=8192,action_id='resource')
    runner=SubprocessRunner(); started=time.monotonic(); response=runner.execute(request)
    result=aggregate(d,response.per_example[0],reward_key=reward_key_for(d))
    assert time.monotonic()-started<3
    assert result.component_results[1].status=='ok'
    if kind!='child': assert result.status=='partial'
    else:
        assert not marker.exists()
        assert result.component_results[0].error.code == 'forbidden_api'
    assert runner.capabilities().filesystem_isolation is False
    assert runner.capabilities().network_isolation is False

@pytest.mark.acceptance('AT-21','AT-23')
def test_sigkill_active_driver_cleans_orphan_worker(config, source, tmp_path):
    marker=tmp_path/'worker-pid'
    d=definition(sources=['def score(e,c):\n while True: pass\n'],api=False)
    script=tmp_path/'script.json'; script.write_text(json.dumps({'math_v2':[{'content':turn(d).content}]}))
    config.role_model('reward_synthesizer').scripted_responses=str(script); config.execution.wall_timeout_s=20; config.execution.cpu_timeout_s=20
    cfg=write_config(config,tmp_path); root=run_path(tmp_path, 'TestRun')
    proc=subprocess.Popen([sys.executable,'-m','rlar_harness','construct','--config',str(cfg),'--input',str(source),'--run-dir',str(root)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    until=time.monotonic()+10
    while not worker_pid(proc.pid) and time.monotonic()<until: time.sleep(.02)
    pid=worker_pid(proc.pid); assert pid
    proc.kill(); proc.communicate(timeout=5)
    for _ in range(50):
        ps=subprocess.run(['ps','-o','stat=','-p',str(pid)],capture_output=True,text=True)
        if not ps.stdout.strip() or ps.stdout.strip().startswith('Z'): break
        time.sleep(.02)
    assert not ps.stdout.strip() or ps.stdout.strip().startswith('Z')
    assert read_run(root)[2]==[]

@pytest.mark.acceptance('AT-01','AT-26')
def test_input_change_refuses_resume_and_exports_preserve_sources(config, source, tmp_path):
    from rlar_harness.trace.export import export_llm_calls
    root=run_path(tmp_path, 'TestRun'); construct_file(config,source,root)
    for target in (source,root/'trace.jsonl',root/'results.jsonl'):
        before=target.read_bytes()
        with pytest.raises(StorageError): export_llm_calls(root,target)
        assert before==target.read_bytes()
    source.write_text(source.read_text()+'{}\n')
    with pytest.raises(StorageError,match='input_changed'): RunDriver(root,resume=True)



def worker_pid(parent):
    result=subprocess.run(['ps','-axo','pid=,ppid=,command='],capture_output=True,text=True)
    for line in result.stdout.splitlines():
        parts=line.strip().split(None,2)
        if len(parts)==3 and parts[1]==str(parent) and 'rlar_harness.runtime.worker' in parts[2]:
            return int(parts[0])
    return None
