"""Read-only, lossless trace projections. Never calls models, tools or runners."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from ..errors import StorageError
from ..llm.adapter import wire_messages
from ..schemas import Message
from ..storage.blobs import BlobStore, atomic_write_bytes
from ..storage.canonical import canonical_json, digest, wire_json, wire_digest
from ..storage.journal import Journal
from ..compat.reading import read_results


def read_run(root):
    root = Path(root)
    blobs = BlobStore(root / 'blobs')
    events = Journal(root / 'trace.jsonl', blobs).scan(verify_blobs=True).events
    results = read_results(root / 'results.jsonl')
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
            if wire_digest(body) != p['request_digest'] or wire_messages(body) != blobs.get_json(p['messages_ref']):
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
    atomic_write_bytes(path, b''.join(wire_json(row) + b'\n' for row in rows))


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
    _, events, _, _ = read_run(root)
    prepared = {e.payload['logical_call_id'] for e in events if e.type == 'llm_prepared'}
    legacy = sum(e.type == 'llm_call_completed' and e.payload.get('logical_call_id') not in prepared for e in events)
    manifest = {'schema_version': 'rlar.llm_export.v1', 'physical_attempts': len(calls),
                'legacy_trace_unavailable': legacy, 'new_requests': 0, 'output': str(output)}
    atomic_write_bytes(Path(str(output) + '.manifest.json'), wire_json(manifest))
    return manifest


def replay(root):
    blobs, events, _, _ = read_run(root)
    previous = {}
    last_state = {}
    role_histories = {}
    for e in events:
        if e.type != 'episode_saved':
            continue
        state = blobs.get_json(e.payload['state_ref'])
        for role, field in (('test_case_synthesizer', 'test_history'), ('reward_synthesizer', 'history')):
            if field in state and state[field]:
                rid = e.episode_id + ':' + role
                old_role = role_histories.get(rid, [])
                if state[field][:len(old_role)] != old_role:
                    raise StorageError('role history was rewritten')
                role_histories[rid] = state[field]
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
        'physical_attempts': len(calls), 'role_episodes': role_histories}


def export_sft(root, output, *, actor_role='reward_synthesizer', format='per_call', max_tokens=131072, include_legacy=False):
    guard_output(root, output)
    if actor_role not in ('test_case_synthesizer', 'reward_synthesizer'):
        raise ValueError('only synthesizer roles are student targets')
    if format == 'final_program_only' and actor_role != 'reward_synthesizer':
        raise ValueError('final_program_only is a reward-only derived view')
    blobs, events, results, manifest = read_run(root)
    legacy_controller = manifest['config']['schema_version'] == 'rlar.config.v1'
    schema_suffix = 'v1' if legacy_controller else 'v2'
    if format not in ('per_call', 'full_trace', 'final_program_only'):
        raise ValueError('unknown SFT format')
    states = {e.episode_id: blobs.get_json(e.payload['state_ref']) for e in events if e.type == 'episode_saved'}
    reward_success = {r.episode_id: r for r in results if r.status == 'success' and not r.reused}
    selected = {eid for eid, s in states.items() if
                (s.get('suite_ref') if actor_role == 'test_case_synthesizer' else eid in reward_success)}
    if manifest['split'] not in ('train', 'training'):
        selected = set()
    valid, excluded, seen = defaultdict(list), Counter(), set()
    policy = manifest['config'].get('v2', {}).get('reward_logic_policy')
    if actor_role == 'reward_synthesizer' and policy != 'self_contained_v1' and not include_legacy:
        excluded['legacy_reward_policy'] = len(selected)
        selected = set()
    for call in llm_calls(root):
        role = call.get('actor_role', 'reward_synthesizer' if legacy_controller else None)
        if role != actor_role or not call.get('training_target_eligible', legacy_controller):
            excluded['non_selected_actor'] += 1
            continue
        if call['episode_id'] not in selected:
            excluded['role_not_training_success'] += 1
            continue
        if not (call['response_complete'] and call['committed_to_history'] and call['schema_valid']):
            excluded['incomplete_invalid_or_uncommitted'] += 1
            continue
        logical = call['logical_call_id']
        if logical in seen:
            excluded['duplicate_logical_response'] += 1
            continue
        seen.add(logical)
        body, text = call['request'], call['response']['text']
        if len(wire_json(body)) + len(text.encode()) > max_tokens:
            excluded['context_window'] += 1
            continue
        suite_ref = states[call['episode_id']].get('suite_ref')
        suite = blobs.get_json(suite_ref) if suite_ref else None
        sample = {'schema_version': 'rlar.sft.' + schema_suffix, 'sample_id': logical, 'episode_id': call['episode_id'],
            'role_episode_id': call.get('role_episode_id', call['episode_id']), 'actor_role': actor_role, 'split': manifest['split'],
            'logical_call_id': logical, 'prompt_messages': wire_messages(body),
            'target_message': {'role': 'assistant', 'content': text},
            'prompt_loss_mask': [False] * len(wire_messages(body)), 'target_trainable': True,
            'loss_scope': 'target_assistant_only', 'model_input_fields': {k: v for k, v in body.items() if k not in ('messages', 'input')},
            'provenance': {'request_digest': call['request_digest'], 'response_ref': call['response_ref'],
                'model_config_ref': call['model_config_ref'], 'suite_digest': suite['suite_digest'] if suite else None,
                'source_levels': sorted({c['evidence_source'] for c in suite['cases']}) if suite else []}}
        valid[call['episode_id']].append(sample)
    rows = []
    for eid, samples in valid.items():
        if format == 'per_call':
            rows.extend(samples)
        elif format == 'full_trace':
            messages = states[eid]['test_history' if actor_role == 'test_case_synthesizer' else 'history']
            targets = {s['logical_call_id'] for s in samples}
            row = {'schema_version': 'rlar.sft.' + schema_suffix, 'sample_id': eid + ':' + actor_role,
                'role_episode_id': eid + ':' + actor_role, 'actor_role': actor_role, 'split': manifest['split'],
                'messages': [{'role': m['role'], 'content': m['content']} for m in messages],
                'message_loss_mask': [m['actor'] == 'assistant' and m['meta'].get('logical_call_id') in targets for m in messages],
                'loss_scope': 'selected_assistants_once', 'provenance': [s['provenance'] for s in samples]}
            if len(wire_json(row['messages'])) <= max_tokens:
                rows.append(row)
            else:
                excluded['context_window'] += 1
        elif format == 'final_program_only':
            rows.append({'schema_version': 'rlar.sft.' + schema_suffix, 'sample_id': eid + ':program', 'actor_role': actor_role,
                'split': manifest['split'], 'target_origin': 'verified_artifact', 'loss_scope': 'program_only',
                'prompt_messages': samples[0]['prompt_messages'],
                'target_program': blobs.get_json(states[eid]['selected']['definition_ref']),
                'provenance': {'reward_key': reward_success[eid].reward_key}})
        else:
            raise ValueError('unknown SFT format')
    prepared = {e.payload['logical_call_id'] for e in events if e.type == 'llm_prepared'}
    excluded['legacy_trace_unavailable'] = sum(e.type == 'llm_call_completed' and e.payload.get('logical_call_id') not in prepared for e in events)
    result = {'schema_version': 'rlar.export.' + schema_suffix, 'actor_role': actor_role, 'format': format,
        'reward_logic_policy': policy or 'legacy_checkers_v1', 'historical_controller': legacy_controller, 'include_legacy': include_legacy,
        'samples': len(rows), 'excluded': dict(excluded), 'new_requests': 0,
        'tool_model_targets': {'harness_verifier': 0, 'rubric_judge': 0},
        'selection': 'role-specific development success; complete valid committed replies; audit never selects training data',
        'split_policy': 'task-family split inherited from run; never randomized per call',
        'token_level_mask_verified': False, 'length_counter': 'conservative_utf8_bytes'}
    _write_jsonl(output, rows)
    atomic_write_bytes(Path(str(output) + '.manifest.json'), wire_json(result))
    return result


def export_feedback(root, output):
    guard_output(root, output)
    blobs, events, _, _ = read_run(root)
    rows = []
    for e in events:
        if e.type != 'execution_evidence':
            continue
        for score in blobs.get_json(e.payload['scores_ref']):
            for component in score['component_results']:
                if component.get('feedback') is not None:
                    rows.append({'schema_version': 'rlar.feedback.v2', 'episode_id': e.episode_id,
                        'evaluation_id': e.payload['evaluation_id'], 'reward_key': score['reward_key'],
                        'example_id': score['example_id'], 'component_id': component['id'],
                        'score': component['score'], 'feedback': component['feedback'],
                        'evidence': component.get('evidence', []), 'training_target_eligible': False})
    _write_jsonl(output, rows)
    return {'rows': len(rows), 'new_requests': 0, 'view': 'policy_facing_feedback'}
