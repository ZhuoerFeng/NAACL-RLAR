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
    return {'schema_version': 'rlar.report.v1', 'run_id': manifest['run_id'], 'profile_kind': manifest['profile_kind'],
        'counts': {'input': manifest.get('input_record_count') or len(results) + len(active),
                   'unprocessed': max(0, (manifest.get('input_record_count') or len(results) + len(active)) - len(results) - len(active)), 'committed': len(results), **{k: statuses[k] for k in ('success', 'failed', 'unvalidated', 'skipped')},
                   'interrupted': len(active)}, 'reuse_count': sum(r.reused for r in results),
        'reuse_rate': sum(r.reused for r in results) / len(results) if results else None,
        'controller_logical_calls': len(logicals), 'physical_requests': len(attempts),
        'transport_retries': sum(max(0, sum(c['logical_call_id'] == key for c in attempts) - 1) for key in logicals),
        'scoring_requests': len(scoring), 'input_tokens': tokens('prompt_tokens'), 'output_tokens': tokens('completion_tokens'),
        'cached_input_tokens': tokens('cached_tokens'), 'unknown_usage_events': unknown + sum(e.payload.get('usage_unknown', False) for e in scoring),
        'cost_usd': None, 'cost_status': 'unknown' if unknown else 'unavailable',
        'partial_cases': partial, 'all_failed_cases': failed, 'mask_distribution': dict(mask),
        'elapsed_wall_seconds': events[-1].ts - events[0].ts if events else 0,
        'latest_run_budget': next((e.payload['run'] for e in reversed(events) if e.type == 'budget_snapshot'), None),
        'latency_seconds_observed': sum(c.get('latency') or 0 for c in calls), 'validation_metrics': metrics,
        'episodes': [{'query_id': r.query_id, 'episode_id': r.episode_id, 'status': r.status, 'reused': r.reused,
                      'stop_reason': r.stop_reason, 'usage': r.usage.model_dump(mode='json')} for r in results],
        'runner_capabilities': manifest['runner_capabilities'],
        'external_integration': {'real_llm': 'not_verified', 'real_rm': 'not_verified', 'isolated_runner': 'not_configured',
                                 'formal_task_packs': 'not_provided', 'qwen_tokenizer': 'not_verified'}}
