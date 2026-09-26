"""Read-only, lossless trace projections. Never calls models, tools or runners."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ..errors import StorageError
from ..schemas import Message
from ..storage.blobs import BlobStore, atomic_write_bytes
from ..storage.canonical import canonical_json, digest
from ..storage.journal import Journal
from ..storage.results import ResultsStore


def read_run(root):
    root = Path(root)
    blobs = BlobStore(root / 'blobs')
    events = Journal(root / 'trace.jsonl', blobs).scan(verify_blobs=True).events
    results = ResultsStore(root / 'results.jsonl').load()
    manifest = json.loads((root / 'run_manifest.json').read_text())
    return blobs, events, results, manifest


def llm_calls(root):
    blobs, events, _, manifest = read_run(root)
    calls = {}
    accepted = {}
    dispatched = set()
    for e in events:
        p = e.payload
        pid = p.get('physical_attempt_id')
        if e.type == 'llm_attempt_prepared':
            body = blobs.get_json(p['request_body_ref'])
            if digest(body) != p['request_digest'] or body['messages'] != blobs.get_json(p['messages_ref']):
                raise StorageError('request/messages digest mismatch')
            calls[pid] = {'schema_version': 'rlar.llm_call.v1', **p, 'request': body,
                'response': None, 'dispatch_status': 'prepared', 'response_complete': False,
                'committed_to_history': False, 'schema_valid': None, 'actions_dispatched': False,
                'usage': None, 'error': None, 'latency': None}
        elif e.type == 'llm_dispatched' and pid in calls:
            calls[pid]['dispatch_status'] = 'unknown'
        elif e.type == 'llm_response' and pid in calls:
            response = blobs.get_json(p['response_ref'])
            calls[pid].update(response=response, response_ref=p['response_ref'],
                response_complete=p['response_complete'], finish_reason=p['finish_reason'],
                provider_request_id=p['provider_request_id'], latency=p['latency'], dispatch_status='returned',
                usage={k: response.get(k) for k in ('prompt_tokens', 'completion_tokens', 'cached_tokens', 'usage_known', 'cost_usd')})
        elif e.type == 'llm_attempt_error' and pid in calls:
            calls[pid].update(dispatch_status=p['status'], error=p, latency=p.get('latency'),
                response=blobs.get_json(p['response_ref']) if p.get('response_ref') else None)
        elif e.type == 'llm_history_committed':
            logical = p['logical_call_id']
            if logical in accepted and accepted[logical] != pid:
                raise StorageError('two responses committed for one logical call')
            accepted[logical] = pid
            if pid in calls:
                calls[pid].update(committed_to_history=p['response_complete'], schema_valid=p['schema_valid'])
        elif e.type == 'llm_actions_dispatched':
            dispatched.add(p['logical_call_id'])
    for call in calls.values():
        call['actions_dispatched'] = call['schema_valid'] is True and call['committed_to_history'] and call['logical_call_id'] in dispatched
    return list(calls.values())


def _write_jsonl(path, rows):
    path = Path(path)
    atomic_write_bytes(path, b''.join(canonical_json(row) + b'\n' for row in rows))


def guard_output(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    protected = {root / n for n in ('trace.jsonl', 'results.jsonl', 'reward_library.jsonl', 'run_manifest.json', '.evaluator-secret', 'run.lock')}
    manifest = json.loads((root / 'run_manifest.json').read_text())
    if manifest.get('input_path'):
        protected.add(Path(manifest['input_path']).resolve())
    protected.update(Path(p).resolve() for p in manifest.get('resource_fingerprints', {}) if Path(p).is_absolute())
    if output in protected or root / 'blobs' in output.parents or root / 'checkpoints' in output.parents:
        raise StorageError('derived output must not overwrite source data or run authority')


def export_llm_calls(root, output):
    guard_output(root, output)
    calls = llm_calls(root)
    _write_jsonl(output, calls)
    return {'physical_attempts': len(calls), 'output': str(output)}


def replay(root):
    blobs, events, _, _ = read_run(root)
    previous = {}
    last_state = {}
    for e in events:
        if e.type != 'episode_saved':
            continue
        state = blobs.get_json(e.payload['state_ref'])
        messages = state['history']
        old = previous.get(e.episode_id, [])
        if messages[:len(old)] != old:
            raise StorageError('replay found rewritten history')
        tail = [{'role': m['role'], 'content': m['content']} for m in messages[2:]]
        if state.get('history_digest') and digest(tail) != state['history_digest']:
            raise StorageError('history hash mismatch')
        previous[e.episode_id] = messages
        last_state[e.episode_id] = state
    calls = llm_calls(root)
    return {'schema_version': 'rlar.replay.v1', 'mode': 'observations', 'new_requests': 0,
        'episodes': {eid: {'messages': messages, 'full_digest': digest([{'role': m['role'], 'content': m['content']} for m in messages]),
                          'state': last_state[eid]['state']} for eid, messages in previous.items()},
        'physical_attempts': len(calls)}


def export_sft(root, output, *, format='per_call', max_tokens=131072):
    guard_output(root, output)
    blobs, events, results, manifest = read_run(root)
    calls = llm_calls(root)
    selected = {r.episode_id: r for r in results if r.status == 'success' and not r.reused
                and manifest['split'] in ('train', 'training')}
    excluded = Counter()
    valid = defaultdict(list)
    seen = set()
    for call in calls:
        if call['episode_id'] not in selected:
            excluded['episode_not_training_success'] += 1
            continue
        if not (call['response_complete'] and call['committed_to_history'] and call['schema_valid']):
            excluded['incomplete_invalid_or_uncommitted'] += 1
            continue
        key = (call['episode_id'], call['logical_call_id'])
        if key in seen:
            excluded['duplicate_logical_response'] += 1
            continue
        seen.add(key)
        target = {'role': 'assistant', 'content': call['response']['text']}
        body = call['request']
        sample = {'schema_version': 'rlar.sft.v1', 'sample_id': ':'.join(key), 'episode_id': key[0],
            'logical_call_id': key[1], 'split': call['split'], 'prompt_messages': body['messages'],
            'target_message': target, 'tools': body.get('tools', []),
            'response_schema': body.get('response_format', body.get('response_schema')),
            'model_input_fields': {k: v for k, v in body.items() if k != 'messages'},
            'loss_scope': 'target_assistant_only', 'prompt_loss_mask': [False] * len(body['messages']),
            'target_trainable': True, 'provenance': {'request_digest': call['request_digest'],
                'response_digest': call['response_ref'].split(':', 1)[1],
                'adapter_version': call['adapter_version'], 'conversion_version': 'json_action.v1',
                'model_config_ref': call['model_config_ref']}}
        # Conservative bytes-based exclusion; no tokenizer or truncation is faked.
        if len(canonical_json(body)) + len(target['content'].encode()) > max_tokens:
            excluded['context_window'] += 1
            continue
        valid[key[0]].append(sample)
    rows = []
    if format == 'per_call':
        rows = [sample for samples in valid.values() for sample in samples]
    elif format == 'full_trace':
        histories = replay(root)['episodes']
        for eid, samples in valid.items():
            targets = {s['logical_call_id'] for s in samples}
            messages = histories[eid]['messages']
            row = {'schema_version': 'rlar.sft.v1', 'sample_id': eid, 'episode_id': eid, 'split': manifest['split'],
                'messages': [{'role': m['role'], 'content': m['content']} for m in messages],
                'message_loss_mask': [m['actor'] == 'assistant' and m['meta'].get('logical_call_id') in targets for m in messages],
                'loss_scope': 'selected_assistants_once', 'provenance': [s['provenance'] for s in samples]}
            if len(canonical_json(row['messages'])) > max_tokens:
                excluded['context_window'] += 1
                continue
            rows.append(row)
    elif format == 'final_program_only':
        # A derived program-only view, explicitly distinct from teacher messages.
        # No automatic submission event or invented assistant utterance is used.
        for eid, samples in valid.items():
            result = selected[eid]
            if result.reward_definition:
                definition = result.reward_definition.model_dump(mode='json')
            else:
                saved = [e for e in events if e.type == 'episode_saved' and e.episode_id == eid]
                state = blobs.get_json(saved[-1].payload['state_ref'])
                definition = blobs.get_json(state['selected']['definition_ref'])
            rows.append({'schema_version': 'rlar.sft.v1', 'sample_id': eid + ':program',
                'episode_id': eid, 'split': manifest['split'],
                'prompt_messages': samples[0]['prompt_messages'], 'target_program': definition,
                'loss_scope': 'program_only', 'target_origin': 'verified_artifact',
                'provenance': {'reward_key': result.reward_key, 'validation_ref': result.validation_ref,
                               'conversion_version': 'program_view.v1'}})
    else:
        raise ValueError('unknown SFT format')
    prepared_logicals = {e.payload['logical_call_id'] for e in events if e.type == 'llm_prepared'}
    legacy = sum(e.type == 'llm_call_completed' and e.payload.get('logical_call_id') not in prepared_logicals for e in events)
    excluded['legacy_trace_unavailable'] += legacy
    export_manifest = {'schema_version': 'rlar.export.v1', 'format': format, 'samples': len(rows),
        'selected_episodes': len(valid), 'excluded': dict(excluded), 'new_requests': 0,
        'selection': 'training split; development success; schema-valid complete committed logical responses; retain failed tests',
        'split_policy': 'run-fixed task-family split inherited by every episode/call; no random call splitting',
        'student': {'model': 'Qwen3-8B', 'revision': None, 'tokenizer_revision': None, 'chat_template': None,
                    'thinking': None, 'tool_encoding': 'json_action.v1', 'token_level_mask_verified': False},
        'max_tokens': max_tokens, 'length_counter': 'conservative_utf8_bytes',
        'note': 'Separate training views; do not concatenate per_call and full_trace by default.'}
    _write_jsonl(output, rows)
    atomic_write_bytes(Path(str(output) + '.manifest.json'), canonical_json(export_manifest))
    return export_manifest
