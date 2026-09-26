"""Persist reviewed decisions by immutable original row/UUID."""
import json
from datetime import datetime, timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parent/'results/nemotron_sample_1000'
def record(entries):
    cache={r['row']:r for r in map(json.loads,(ROOT/'candidate_cache.jsonl').open())}
    path=ROOT/'review_decisions.jsonl'
    existing={r['uuid']:r for r in map(json.loads,path.open())} if path.exists() else {}
    for row,decision,full,reason in entries:
        r=cache[row]
        existing[r['uuid']]={'uuid':r['uuid'],'row':row,'decision':decision,'full_conversation_reviewed':full,'reason':reason,'reviewer':'Codex semantic/flag inspection; not an external factual audit','reviewed_utc':datetime.now(timezone.utc).isoformat()}
    with path.open('w') as f:
        for r in sorted(existing.values(),key=lambda x:x['row']):f.write(json.dumps(r,ensure_ascii=False)+'\n')
    print('Saved decisions:',len(entries),'Total:',len(existing))
