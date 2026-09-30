"""Bounded test-case and reward synthesis state machines per query."""
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
The exact top-level shape is {"actions": [{"id": "a1", "tool": "test_reward", "arguments": {...}}]}.
Every action needs a unique id; never return a bare tool/arguments object. The tool argument
schemas follow below. These are actions inside message.content, not provider tool calls.
Every definition must explicitly set runtime_contract.environment_ref to the exact
runtime_fingerprint string in the run prefix below. Do not use the schema placeholder
configured_at_run_start. runtime_contract.python_version is a Python version (use 3.11+),
not the runtime_fingerprint. Use the field names and enum values in the tool schemas.
Each test_reward supplies the COMPLETE definition: components in planned order, each with
id, criterion, all Python source, normalization and required_apis. Generate plan and code
in this single decision. score(example, context) returns {raw_score: finite number, feedback: string, evidence: list}; errors are not
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
    instructions = RUN_INSTRUCTIONS
    instructions += ('\nSet runtime_contract.reward_logic_policy="self_contained_v1". '
        'Implement all task-specific parsing, extraction, validation, comparison and raw scoring in your source. '
        'There are no native task checkers. Use only the general-purpose dependencies listed in scoring_abi. '
        'Verifiable components require no context APIs. Rubric alone may call the raw judge utility; '
        'implement its response parser yourself. Local helper functions are allowed. Do not hardcode sample answers.')
    instructions += ('\nYou are the reward synthesizer. Use rlar.reward.v2 and scoring_abi=v2. '
        'Capabilities are fixed by the task pack. Read frozen_suite before designing the reward. '
        'Components require kind and capability_ids. For rubric use context.judge_spec as the single rubric authority; '
        'call context.call_llm_api(message, context.judge_spec["model_ref"]) once, then parse the raw string in the declared parser. '
        'Frozen suite labels, required flags and relations cannot be edited. Never embed candidate answers or IDs in source.')
    prefix = Message(role='system', actor='run_prefix', content=instructions + '\n' + json.dumps({
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
    if s['state'] in ('SELECTED', 'FAILED'):
        return s
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
        save({'success': 'SELECTED', 'failed': 'FAILED'}[status])
        return s

    def budget_finish(reason):
        candidates = [ToolResult.model_validate(x) for x in s.get('eligible', [])]
        selected = c.dispatcher.select(candidates, all_eligible=True)
        return finish('success' if selected else 'failed', reason, selected)

    if s['state'] in ('SELECTED', 'FAILED'):
        return s
    try:
        while True:
            c.deadline.check('episode')
            if s['state'] in ('REUSE_CHECK', 'OBSERVATIONS_READY', 'INPUT_VALIDATED'):
                if s.get('repeats', 0) >= c.config.budget.no_progress_threshold:
                    return budget_finish('quality_not_met')
                if s['step'] >= c.config.synthesis.max_reward_decisions:
                    return budget_finish('budget_exhausted')
                if s.get('reward_attempts', 0) >= c.config.synthesis.reward_synthesis_attempts:
                    return budget_finish('verification_unavailable' if (s.get('current') or {}).get('insufficient_evidence') else 'quality_not_met')
                s['step'] += 1
                if s['step'] > 1:
                    c.budget.debit_once(f"{s['episode_id']}:revision:{s['step']}", {'revisions': 1.0})
                s['logical_call_id'] = f"{s['episode_id']}:reward_synthesizer:{s['step']}"
                save('LLM_PENDING')
            logical = s['logical_call_id']
            if s['state'] == 'LLM_PENDING':
                result = c.llm.call(history, logical_call_id=logical)
                if result.status in ('failed', 'unknown'):
                    if result.error.category == 'auth':
                        raise HarnessError(result.error.message, code=result.error.code)
                    return finish('failed', 'infrastructure_error')
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
                if error is not None or any((a.tool == 'test_reward' for a in result.proposed_actions)):
                    s['reward_attempts'] = s.get('reward_attempts', 0) + max(1, sum(a.tool == 'test_reward' for a in result.proposed_actions))
                    if s['reward_attempts'] > c.config.synthesis.reward_synthesis_attempts:
                        return budget_finish('quality_not_met')
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
                if any(((r.result or {}).get('verification_unavailable') for r in results)):
                    return finish('failed', 'verification_unavailable')
                if selected:
                    c.journal.append('automatic_finalization', {'reward_key': selected['reward_key']}, episode_id=s['episode_id'])
                    return finish('success', 'validated', selected)
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
        return budget_finish('budget_exhausted')
    except HarnessError as exc:
        if exc.code == 'environment_unavailable':
            return finish('failed', 'infrastructure_error')
        raise


TEST_INSTRUCTIONS = '''You are the test case synthesizer. Return one complete SuiteDraft JSON.
Use only the task specification, allowed references and evidence policy. Never read or infer a candidate reward.
Pointwise pass/fail labels describe the candidate answer in the declared capability scope.
Ranking relations must explicitly state all comparisons, with a basis for each relation.
Only the program admits and freezes a suite. Do not output digests, signatures, or admission status.
Model-inferred relations must be labelled model_inferred. Do not claim human/objective evidence without its configured source.
'''


def synthesize_tests(record, context, client, snapshot):
    """Finite, role-local draft loop; its own success is committed before reward work."""
    from .schemas import SuiteDraft, FrozenSuite
    from .evaluation.suite import admit_suite, assert_frozen, example_input
    from .tools.resources import Resource
    c, s = context, context.state
    if s['state'] == 'FAILED':
        return
    if not s.get('suite_ref'):
        if not s.get('test_history'):
            h = [Message(role='system', actor='run_prefix', content=TEST_INSTRUCTIONS + '\n' + json.dumps(SuiteDraft.model_json_schema())),
                 Message(role='user', actor='episode_prefix', content=json.dumps({
                     'query': c.dispatcher.resources.get('query').loader(),
                     'task_contract': c.dispatcher.pack.task_contract,
                     'capabilities': [cap.model_dump(mode='json') for cap in c.dispatcher.pack.capabilities],
                     'suite_policy': c.dispatcher.pack.suite_policy.model_dump(mode='json'),
                     'allowed_resources': c.dispatcher.pack.resources,
                     'remaining_budget': c.budget.remaining_all()}, ensure_ascii=False))]
            s['test_history'] = [m.model_dump(mode='json') for m in h]
        h = [Message.model_validate(m) for m in s['test_history']]
        history = EpisodeHistory.restore((h[0],), (h[1],), h[2:])
        def save():
            s['test_history'] = [m.model_dump(mode='json') for m in history.messages()]
            c.save()
        try:
            while not s.get('suite_ref'):
                # Pending call number is persisted before dispatch, never reset on resume.
                if s['state'] != 'TEST_LLM_PENDING':
                    attempt = s.get('test_attempt', 0) + 1
                    if attempt > c.config.synthesis.test_synthesis_attempts:
                        s.update(state='FAILED', status='failed', stop_reason='test_suite_unavailable')
                        save()
                        return
                    s.update(test_attempt=attempt, state='TEST_LLM_PENDING')
                    save()
                logical = f"{s['episode_id']}:test_case_synthesizer:{s['test_attempt']}"
                result = client.call(history, logical_call_id=logical)
                if result.status in ('failed', 'unknown') and result.error.category == 'auth':
                    raise HarnessError(result.error.message, code=result.error.code)
                if result.status in ('failed', 'unknown'):
                    s.update(state='FAILED', status='failed', stop_reason='infrastructure_error')
                    save()
                    return
                error, suite = None, None
                try:
                    if result.status != 'complete':
                        raise ValueError('suite output truncated')
                    draft = SuiteDraft.model_validate_json(result.assistant_text)
                    suite = admit_suite(draft, record, c.dispatcher.pack,
                        generation_config_ref=digest({'model': client.model_config, 'prompt': TEST_INSTRUCTIONS,
                                                      'policy': c.dispatcher.pack.suite_policy}), blobs=c.blobs)
                except (ValueError, TypeError) as exc:
                    error = str(exc)[:c.config.logging.max_observation_chars]
                client.accept(history, result, logical, schema_valid=error is None)
                if result.status == 'complete':
                    history.append_assistant(result.assistant_text, meta={'logical_call_id': logical,
                        'actor_role': 'test_case_synthesizer', 'schema_valid': error is None})
                    history.tail[-1].trainable = error is None
                if error:
                    history.append_observation(json.dumps({'admission_error': error, 'remaining_budget': c.budget.remaining_all()}))
                    s['state'] = 'SYNTHESIZE_TESTS'
                    save()
                    continue
                s['suite_ref'] = c.blobs.put_json(suite.model_dump(mode='json'))
                s['state'] = 'SUITE_FROZEN'
                history.append_observation(json.dumps({'admitted': True, 'suite_digest': suite.suite_digest}))
                # One checkpoint atomically binds suite and accepted role history.
                save()
                c.journal.append('suite_frozen', {'suite_ref': s['suite_ref'], 'suite_digest': suite.suite_digest,
                    'actor_role': 'test_case_synthesizer', 'role_episode_id': s['episode_id'] + ':test_case_synthesizer'},
                    episode_id=s['episode_id'])
                fault('suite_after_freeze')
        except (BudgetExhausted, DeadlineExceeded):
            s.update(state='FAILED', status='failed', stop_reason='budget_exhausted')
            save()
            return
    suite = FrozenSuite.model_validate(c.blobs.get_json(s['suite_ref']))
    assert_frozen(suite)
    c.dispatcher.suite = suite
    c.dispatcher.resources.add(Resource('frozen_suite', 'json', 'Admitted development suite and basis',
                                       lambda: suite.model_dump(mode='json')))
    if not s['history']:
        s['history'] = initial_history(record, c.dispatcher.pack, snapshot, c.dispatcher.resources, c.config, c.budget)
        s['state'] = 'REUSE_CHECK'
        c.save()
