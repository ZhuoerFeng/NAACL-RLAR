"""One-command end-to-end offline demonstration, with real fixture execution."""
import argparse
import json
from pathlib import Path
from rlar_harness.config import load_config
from rlar_harness.driver import construct_file, resolve_config
from rlar_harness.cli import audit_run, score_file
from rlar_harness.trace.export import export_llm_calls, export_sft, replay
from rlar_harness.trace.report import report
from rlar_harness.storage.blobs import atomic_write_bytes
from rlar_harness.storage.canonical import canonical_json

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(); p.add_argument('--run-dir',type=Path,default=ROOT/'runs/demo'); args=p.parse_args()
    config=resolve_config(load_config(ROOT/'configs/offline_demo.yaml'),ROOT/'configs')
    results=construct_file(config,config.data.input_path,args.run_dir)
    score_file(args.run_dir,ROOT/'examples/candidates.jsonl',args.run_dir/'scores.jsonl')
    audit_run(args.run_dir,ROOT/'examples/audit_suite.json',args.run_dir/'audit/prototype.json',allow_prototype=True)
    export_llm_calls(args.run_dir,args.run_dir/'exports/llm_calls.jsonl')
    for format in ('per_call','full_trace','final_program_only'):
        export_sft(args.run_dir,args.run_dir/f'exports/sft_{format}.jsonl',format=format)
    atomic_write_bytes(args.run_dir/'replay.json',canonical_json(replay(args.run_dir)))
    atomic_write_bytes(args.run_dir/'report.json',canonical_json(report(args.run_dir)))
    summary=[{'query_id':r.query_id,'status':r.status,'reused':r.reused,
        'controller_decisions':r.usage.controller_logical_calls,'reward_key':r.reward_key} for r in results]
    assert [r['controller_decisions'] for r in summary]==[2,0,1]
    assert all(r['status']=='success' for r in summary)
    atomic_write_bytes(args.run_dir/'demo_summary.json',canonical_json(summary))
    print(json.dumps({'run_dir':str(args.run_dir.resolve()),'results':summary},indent=2))

if __name__=='__main__': main()
