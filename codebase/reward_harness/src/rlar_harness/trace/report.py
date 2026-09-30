"""All-denominator accounting, including failures, retries and unknown charges."""
from collections import Counter, defaultdict
from .export import llm_calls, read_run


def report(root):
    blobs, events, results, manifest = read_run(root)
    calls = llm_calls(root)
    statuses = Counter(r.status for r in results)
    logicals = {e.payload['logical_call_id'] for e in events if e.type == 'llm_prepared'}
    attempts = [c for c in calls if c['dispatch_status'] != 'prepared']
    scoring = [e for e in events if e.type == 'scoring_request' and e.payload.get('status') != 'rejected']
    mask = Counter()
    partial = failed = 0
    metrics = []
    for e in events:
        if e.type != 'evaluation_result':
            continue
        validation = blobs.get_json(e.payload['validation_ref'])
        m = validation['metrics']
        partial += m['partial_case_count']; failed += m['all_failed_case_count']
        mask.update(m['component_mask_distribution'])
        metrics.append({'episode_id': e.episode_id, 'reward_key': validation['reward_key'], 'eligible': validation['eligible'], 'metrics': m})
    unknown = sum(c['usage'] is None or not c['usage']['usage_known'] for c in attempts)
    def tokens(field):
        values = [c['usage'][field] for c in attempts if c['usage'] and c['usage'].get(field) is not None]
        return sum(values) if values else None
    active = {e.episode_id for e in events if e.type == 'episode_saved'} - {r.episode_id for r in results}
    return {'schema_version': 'rlar.report.v2' if manifest['config']['schema_version'] == 'rlar.config.v2' else 'rlar.report.v1', 'run_id': manifest['run_id'], 'profile_kind': manifest['profile_kind'],
        'counts': {'input': manifest.get('input_record_count') or len(results) + len(active),
                   'unprocessed': max(0, (manifest.get('input_record_count') or len(results) + len(active)) - len(results) - len(active)), 'committed': len(results), **{k: statuses[k] for k in ('success', 'failed', 'unvalidated', 'skipped')},
                   'interrupted': len(active)}, 'reuse_count': sum(r.reused for r in results),
        'reuse_rate': sum(r.reused for r in results) / len(results) if results else None,
        'controller_logical_calls': len({c['logical_call_id'] for c in calls if not c.get('actor_role')}), 'physical_requests': len(attempts),
        'transport_retries': sum(max(0, sum(c['logical_call_id'] == key for c in attempts) - 1) for key in logicals),
        'scoring_requests': len(scoring) + sum(c.get('actor_role') == 'rubric_judge' for c in attempts),
        'actor_usage': actor_usage(calls), 'synthesis_statistics': synthesis_statistics(blobs, events), 'input_tokens': tokens('prompt_tokens'), 'output_tokens': tokens('completion_tokens'),
        'cached_input_tokens': tokens('cached_tokens'), 'unknown_usage_events': unknown + sum(e.payload.get('usage_unknown', False) for e in scoring),
        'cost_usd': None, 'cost_status': 'unknown' if unknown else 'unavailable',
        'partial_cases': partial, 'all_failed_cases': failed, 'mask_distribution': dict(mask),
        'elapsed_wall_seconds': events[-1].ts - events[0].ts if events else 0,
        'latest_run_budget': next((e.payload['run'] for e in reversed(events) if e.type == 'budget_snapshot'), None),
        'latency_seconds_observed': sum(c.get('latency') or 0 for c in calls), 'validation_metrics': metrics,
        'episodes': [{'query_id': r.query_id, 'episode_id': r.episode_id, 'status': r.status, 'outcome': 'done' if r.status == 'success' else r.status, 'reused': r.reused,
                      'stop_reason': r.stop_reason, 'usage': r.usage.model_dump(mode='json')} for r in results],
        'runner_capabilities': manifest['runner_capabilities'],
        'external_integration': {'real_llm': 'not_verified', 'real_rm': 'not_verified', 'isolated_runner': 'not_configured',
                                 'formal_task_packs': 'not_provided', 'qwen_tokenizer': 'not_verified'}}


def actor_usage(calls):
    grouped = defaultdict(list)
    for call in calls:
        grouped[call.get('actor_role', 'legacy_controller')].append(call)
    return {role: {'logical_calls': len({c['logical_call_id'] for c in group}),
        'physical_attempts': sum(c['dispatch_status'] != 'prepared' for c in group),
        'input_tokens': sum((c.get('usage') or {}).get('prompt_tokens') or 0 for c in group) if any((c.get('usage') or {}).get('prompt_tokens') is not None for c in group) else None,
        'output_tokens': sum((c.get('usage') or {}).get('completion_tokens') or 0 for c in group) if any((c.get('usage') or {}).get('completion_tokens') is not None for c in group) else None,
        'unknown_usage_events': sum(not (c.get('usage') or {}).get('usage_known') for c in group),
        'latency_seconds': sum(c.get('latency') or 0 for c in group), 'cost_usd': None,
        'cost_status': 'unavailable'} for role, group in grouped.items()}


def synthesis_statistics(blobs, events):
    states = {e.episode_id: blobs.get_json(e.payload['state_ref']) for e in events if e.type == 'episode_saved'}
    rows = []
    for eid, state in states.items():
        if not state.get('suite_ref'):
            continue
        suite = blobs.get_json(state['suite_ref'])
        current = state.get('selected') or state.get('current') or {}
        definition = blobs.get_json(current['definition_ref']) if current.get('definition_ref') else None
        distribution = Counter(cap for c in (definition or {}).get('components', []) for cap in c['capability_ids'])
        rows.append({'episode_id': eid, 'suite_digest': suite['suite_digest'],
            'examples': len(suite['examples']), 'cases': len(suite['cases']),
            'relations': sum(len(c['relations']) for c in suite['cases']),
            'case_categories': dict(Counter(c['expected_label'] if c['kind'] == 'pointwise' else 'ranking' for c in suite['cases'])),
            'required_cases': sum(c['required'] for c in suite['cases']),
            'source_levels': dict(Counter(c['evidence_source'] for c in suite['cases'])),
            'components': len((definition or {}).get('components', [])), 'capability_coverage': dict(distribution),
            'test_synthesis_attempts': state.get('test_attempt', 0),
            'reward_synthesis_attempts': state.get('reward_attempts', 0),
            'reward_revisions': max(0, state.get('reward_attempts', 0)-1)})
    return rows
