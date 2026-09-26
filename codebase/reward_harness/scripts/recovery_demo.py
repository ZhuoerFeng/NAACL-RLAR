"""Kill actual CLI processes at durable boundaries and save reproducible evidence."""
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from rlar_harness.trace.export import read_run, llm_calls, export_llm_calls, export_sft, replay
from rlar_harness.storage.blobs import atomic_write_bytes
from rlar_harness.storage.canonical import canonical_json

ROOT=Path(__file__).resolve().parents[1]
POINTS=('llm_before_dispatch','llm_after_dispatch','llm_after_response','tool_after_result',
        'after_library_before_result','after_result_before_checkpoint')

def command(*args,env=None):
    return subprocess.run([sys.executable,'-m','rlar_harness',*map(str,args)],capture_output=True,text=True,timeout=30,env=env)

def main():
    p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,default=ROOT/'runs/recovery');args=p.parse_args()
    evidence=[]
    for point in POINTS:
        root=args.output_dir/point
        first=command('construct','--config',ROOT/'configs/offline_demo.yaml','--run-dir',root,
                      env={**os.environ,'RLAR_TEST_KILL_AT':point})
        assert first.returncode==-signal.SIGKILL,(point,first.stderr)
        before_calls=llm_calls(root)
        resumed=command('resume','--run-dir',root)
        assert resumed.returncode==0,(point,resumed.stderr)
        data=(root/'results.jsonl').read_bytes()
        second=command('resume','--run-dir',root)
        assert second.returncode==0 and data==(root/'results.jsonl').read_bytes()
        blobs,events,results,manifest=read_run(root)
        assert len(results)==3 and len({r.job_key for r in results})==3
        assert [r.usage.controller_logical_calls for r in results]==[2,0,1]
        calls=llm_calls(root)
        export_llm_calls(root,root/'exports/llm_calls.jsonl')
        sft=export_sft(root,root/'exports/sft_per_call.jsonl')
        histories=replay(root)
        assert sft['samples']==3
        evidence.append({'fault_point':point,'kill_exit_code':first.returncode,'resume_exit_code':resumed.returncode,
            'committed_results':len(results),'duplicate_results':0,'noop_resume_unchanged':True,
            'physical_attempts_before_kill':len(before_calls),'physical_attempts_after_resume':len(calls),
            'unknown_attempts':sum(c['dispatch_status']=='unknown' for c in calls),
            'assistant_targets':sft['samples'],'controller_decisions':[r.usage.controller_logical_calls for r in results],
            'deadline_utc':manifest['deadline_utc'],'result_sha256':hashlib.sha256(data).hexdigest(),
            'run_dir':str(root.resolve()),'replay_verified':histories['new_requests']==0})
    atomic_write_bytes(args.output_dir/'recovery_evidence.json',canonical_json(evidence))
    print(json.dumps(evidence,indent=2))

if __name__=='__main__':main()
