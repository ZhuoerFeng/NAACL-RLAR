"""One explicit, serial controller state machine per query."""
from __future__ import annotations

import json
from dataclasses import dataclass

from .durability import fault
from .errors import BudgetExhausted, DeadlineExceeded, HarnessError
from .llm.history import EpisodeHistory, truncate_observation
from .schemas import Message, ProposedAction, ToolResult
from .storage.blobs import atomic_write_bytes
from .storage.canonical import canonical_json, digest
from .tools.dispatcher import TOOL_SCHEMAS, validate_batch


RUN_INSTRUCTIONS = '''Construct a reusable task-contract reward. Return one JSON action envelope.
Each test_reward supplies the COMPLETE definition: components in planned order, each with
id, criterion, all Python source, normalization and required_apis. Generate plan and code
in this single decision. score(example, context) returns a finite scalar; errors are not
zero. checklist is the equal mean of successfully executed components. Never embed query
answers, sample IDs or labels in source. Use only declared APIs and normalization maps.
All observations use a user-role JSON envelope from the harness. test_reward(on_pass=submit)
automatically finalizes only after complete trusted development validation. submit_reward
must be alone in a batch. A batch's results are returned in declared order after its barrier.
No separate completion message is needed. Audit resources are unavailable.
'''


@dataclass
class EpisodeContext:
    state: dict
    config: object
    dispatcher: object
    llm: object
    budget: object
    journal: object
    blobs: object
    run_dir: object
    deadline: object

    def save(self):
        ref = self.blobs.put_json(self.state)
        self.journal.append('episode_saved', {'state_ref': ref, 'state': self.state['state']},
                            episode_id=self.state['episode_id'], query_id=self.state['query_id'])
        atomic_write_bytes(self.run_dir / 'checkpoints/latest.json', canonical_json({
            'schema_version': 'rlar.checkpoint.v1', 'state_ref': ref,
            'journal_seq': self.journal.seq, 'deadline_utc': self.deadline.absolute_utc,
            'run_budget': self.budget.run.to_json(), 'episode_budget': self.budget.episode.to_json()}))


def initial_history(query, pack, snapshot, resources, config, budget):
    prefix = Message(role='system', actor='run_prefix', content=RUN_INSTRUCTIONS + '\n' + json.dumps({
        'tools': TOOL_SCHEMAS, 'reward_abi': 'score(example, context)',
        'runtime_fingerprint': config.execution.runtime_fingerprint,
        'model_cards': resources.get('model_catalogue').loader()}, ensure_ascii=False, sort_keys=True))
    public = {k: v for k, v in query.model_dump(mode='json').items()
              if k in set(pack.permitted_inputs) | {'query_id', 'query', 'task_profile_id', 'reward_mode'}}
    episode = Message(role='user', actor='episode_prefix', content=json.dumps({
        'query': public, 'task_contract': resources.get('task_contract').loader(),
        'reward_mode': query.reward_mode or config.construction.reward_mode,
        'initial_budget': budget.remaining_all(), 'library_snapshot': snapshot.snapshot_id,
        'library': resources.get('library_index').loader(), 'resources': resources.catalogue()},
        ensure_ascii=False, sort_keys=True))
    return [prefix.model_dump(mode='json'), episode.model_dump(mode='json')]


def construct_one(record, context: EpisodeContext):
    c, s = context, context.state
    history = EpisodeHistory.restore((Message.model_validate(s['history'][0]),),
        (Message.model_validate(s['history'][1]),), [Message.model_validate(m) for m in s['history'][2:]])

    def save(state):
        s['state'] = state
        s['history'] = [m.model_dump(mode='json') for m in history.messages()]
        s['prefix_digest'], s['history_digest'] = history.prefix_digest(), history.history_digest()
        s['history_cursor'] = history.cursor
        c.save()

    def observe(value):
        full = json.dumps(value, ensure_ascii=False, sort_keys=True)
        raw_ref = c.blobs.put_text(full)
        text, truncated = truncate_observation(full, c.config.logging.max_observation_chars,
                                               c.config.logging.truncation_marker)
        history.append_observation(text, meta={'raw_ref': raw_ref, 'truncated': truncated})

    def finish(status, reason, selected=None):
        s.update(status=status, stop_reason=reason, selected=selected)
        save({'success': 'SELECTED', 'unvalidated': 'UNVALIDATED', 'failed': 'FAILED'}[status])
        return s

    def budget_finish(reason):
        candidates = [ToolResult.model_validate(x) for x in s.get('eligible', [])]
        selected = c.dispatcher.select(candidates, all_eligible=True)
        return finish('success' if selected else 'failed', reason, selected)

    if s['state'] in ('SELECTED', 'UNVALIDATED', 'FAILED'):
        return s
    try:
        while True:
            c.deadline.check('episode')
            if s['state'] in ('REUSE_CHECK', 'OBSERVATIONS_READY', 'INPUT_VALIDATED'):
                if s.get('repeats', 0) >= c.config.budget.no_progress_threshold:
                    return budget_finish('no_progress')
                s['step'] += 1
                if s['step'] > 1:
                    c.budget.debit_once(f"{s['episode_id']}:revision:{s['step']}", {'revisions': 1.0})
                s['logical_call_id'] = f"{s['episode_id']}:call:{s['step']}"
                save('LLM_PENDING')
            logical = s['logical_call_id']
            if s['state'] == 'LLM_PENDING':
                result = c.llm.call(history, logical_call_id=logical)
                if result.status in ('failed', 'unknown'):
                    raise HarnessError(result.error.message, code=result.error.code)
                error = result.parse_error
                if error is None:
                    try:
                        validate_batch(result.proposed_actions)
                        for action in result.proposed_actions:
                            if action.tool == 'test_reward':
                                actual_mode = action.arguments['definition']['mode']
                                if actual_mode != (record.reward_mode or c.config.construction.reward_mode):
                                    raise ValueError('reward mode is fixed before construction')
                    except (ValueError, KeyError) as exc:
                        from .schemas import StructuredError
                        error = StructuredError(category='protocol', code='invalid_action_schema',
                            phase='dispatch', retry_owner='agent', message=str(exc))
                if not c.llm.accept(history, result, logical, schema_valid=error is None):
                    return budget_finish('stale_history_cursor')
                if result.status == 'complete':
                    history.append_assistant(result.assistant_text, meta={'logical_call_id': logical,
                        'physical_attempt_id': result.trace_ref, 'schema_valid': error is None})
                    history.tail[-1].trainable = error is None
                if error:
                    observe({'protocol_error': error.model_dump(mode='json'), 'remaining_budget': c.budget.remaining_all()})
                    signature = error.code
                    s['repeats'] = s.get('repeats', 0) + 1 if s.get('signature') == signature else 1
                    s['signature'] = signature
                    save('OBSERVATIONS_READY')
                    if c.config.construction.strategy == 'single_completion':
                        return finish('failed', 'single_completion_invalid_output')
                    continue
                s['pending'] = [a.model_dump(mode='json') for a in result.proposed_actions]
                save('ACTIONS_PENDING')
            if s['state'] == 'ACTIONS_PENDING':
                actions = [ProposedAction.model_validate(a) for a in s['pending']]
                results = c.dispatcher.batch(actions, logical)
                c.journal.append('llm_actions_dispatched', {'logical_call_id': logical}, episode_id=s['episode_id'])
                s['eligible'] += [r.model_dump(mode='json') for r in results if (r.result or {}).get('eligible')]
                selected = c.dispatcher.select(results, all_eligible=c.config.construction.strategy == 'single_completion')
                drafts = [r.result for r in results if r.result and r.result.get('definition_ref')]
                if drafts:
                    s['current'] = drafts[-1]
                # No finalization event is ever represented as assistant text.
                observe({'type': 'harness_observation.v1', 'results': [r.model_dump(mode='json') for r in results],
                         'remaining_budget': c.budget.remaining_all()})
                if selected:
                    c.journal.append('automatic_finalization', {'reward_key': selected['reward_key']}, episode_id=s['episode_id'])
                    return finish('success', 'validated', selected)
                if drafts and drafts[-1].get('assurance') == 'static':
                    return finish('unvalidated', 'no_dev_suite', drafts[-1])
                if c.config.construction.strategy == 'single_completion':
                    return finish('failed', 'single_completion_validation_failed')
                signature = digest([{'tool': r.tool, 'key': (r.result or {}).get('reward_key'),
                                     'error': r.error.code if r.error else None,
                                     'eligible': (r.result or {}).get('eligible'),
                                     'resource': (r.result or {}).get('content_digest')} for r in results])
                s['repeats'] = s.get('repeats', 0) + 1 if s.get('signature') == signature else 1
                s['signature'] = signature
                save('OBSERVATIONS_READY')
    except (BudgetExhausted, DeadlineExceeded) as exc:
        return budget_finish(exc.code)
