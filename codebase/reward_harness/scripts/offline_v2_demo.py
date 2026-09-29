"""Offline double-synthesizer demo. Reward Python executes; all LLMs are fixtures."""
import argparse
import json
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from rlar_harness.config import load_config
from rlar_harness.driver import construct_file, resolve_config
from rlar_harness.trace.export import export_llm_calls, export_sft, export_feedback, replay
from rlar_harness.trace.report import report
from rlar_harness.storage.blobs import atomic_write_bytes
from rlar_harness.storage.canonical import canonical_json

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--run-dir', type=Path, default=None)
args = p.parse_args()
run = args.run_dir or root / 'runs' / (datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y_%m_%d_%H_%M_') + 'HarnessV2Demo')
config = resolve_config(load_config(root / 'configs/offline_v2.yaml'), root / 'configs')
rows = construct_file(config, config.data.input_path, run)
exports = run / 'exports'
exports.mkdir(exist_ok=True)
export_llm_calls(run, exports / 'llm_calls.jsonl')
views = {}
for actor in ('test_case_synthesizer', 'reward_synthesizer'):
    for fmt in ('per_call', 'full_trace'):
        views[actor + ':' + fmt] = export_sft(run, exports / (actor + '_' + fmt + '.jsonl'), actor_role=actor, format=fmt)
export_feedback(run, exports / 'language_feedback.jsonl')
atomic_write_bytes(run / 'report.json', canonical_json(report(run)))
atomic_write_bytes(run / 'replay.json', canonical_json(replay(run)))
summary = {'outcomes': ['done' if r.status == 'success' else r.status for r in rows],
    'teacher_services': 'scripted_fixture', 'verifier_quality': 'not_evaluated',
    'reward_execution': 'actual_python', 'isolation': 'behavioral_prototype', 'sft_views': views}
atomic_write_bytes(run / 'demo_summary.json', canonical_json(summary))
print(json.dumps({'run_dir': str(run.resolve()), **summary}, indent=2))
