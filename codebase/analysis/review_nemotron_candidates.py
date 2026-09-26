#!/usr/bin/env python3
"""Display complete records or focused language triage; never execute dataset text."""
import argparse
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent / "results/nemotron_sample_1000"

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('mode',choices=['full','language','brief','stats'])
    parser.add_argument('--rows',default='')
    parser.add_argument('--start',type=int,default=0)
    parser.add_argument('--limit',type=int,default=10)
    args=parser.parse_args()
    pending=[json.loads(l) for l in (ROOT/'pending_review.jsonl').open()]
    decisions={r['uuid']:r for r in map(json.loads,(ROOT/'review_decisions.jsonl').open())}
    pending=[r for r in pending if not (r['uuid'] in decisions and (
        decisions[r['uuid']]['decision']=='reject' or (
        decisions[r['uuid']]['decision']=='accept' and (
        'random_100_full_conversation_review' not in r['review_triggers'] or
        decisions[r['uuid']].get('full_conversation_reviewed') is True))))]
    if args.rows:
        wanted={int(v) for v in args.rows.split(',')}
        pending=[r for r in pending if r['row'] in wanted]
    elif args.mode=='language':
        pending=[r for r in pending if set(r['review_triggers'])<= {'language_needs_review','non_latin_script_in_user_review_only'}]
    elif args.mode=='full':
        pending=sorted(pending,key=lambda r: ('random_100_full_conversation_review' not in r['review_triggers'],r['row']))
    if args.mode=='stats':
        print([(r['row'],sum(len(m['content']) for m in r['original']['messages']),r['review_triggers']) for r in pending]);return
    for r in pending[args.start:args.start+args.limit]:
        print('\nRECORD',r['row'],r['uuid'],'turns',r['user_turns'],'flags',r['review_triggers'])
        for i,m in enumerate(r['original']['messages']):
            if not m['content'].strip():continue
            text=m['content']
            if args.mode=='language':
                checks=[l for l in r['language_checks'] if l['message_index']==i and l.get('hint')not in ['en','neutral']]
                print('LANGUAGE',checks)
                limit=1600 if checks or m['role']=='user' else 500
                if len(text)>limit:
                    text=text[:limit//2]+'\n[EXCERPT: middle omitted for language triage]\n'+text[-limit//2:]
            elif args.mode=='brief': text=text[:600]+(' [TRUNCATED]' if len(text)>600 else '')
            print(f'{i} {m["role"]}: {text}')
        if r['word_constraints']:print('WORD CONSTRAINTS',r['word_constraints'])
        print('END RECORD',r['row'])

if __name__=='__main__':main()
