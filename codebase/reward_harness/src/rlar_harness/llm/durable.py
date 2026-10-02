"""Durable llm_call: final wire snapshots, bounded physical retries, replay.

The adapter build/send boundary is explicit: the EXACT persisted body is passed
into send_prepared. HTTP adapters must not rebuild it or retry internally.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict

from ..budget import Deadline
from ..config import ModelConfig, RetryConfig
from ..durability import fault, watchdog
from ..errors import BudgetExhausted, Code, DeadlineExceeded, StorageError
from ..schemas import LLMRequest, LLMResult, StructuredError, Usage
from ..storage.canonical import digest, wire_digest
from .adapter import ProviderError, ProviderResponse, wire_messages
from .history import estimate_tokens
from .protocol import parse_actions, ParseOutcome

TOOLS = frozenset({'read_resource', 'test_reward', 'submit_reward'})


class DurableLLMClient:
    def __init__(self, adapter, model_config: ModelConfig, *, budget, journal, blobs,
                 episode_id, run_id, task_id, split, retry=None, deadline=None,
                 actor_role=None, parent_job_id=None, purpose=None):
        self.adapter, self.model_config = adapter, model_config
        self.budget, self.journal, self.blobs = budget, journal, blobs
        self.episode_id = episode_id
        self.identity = dict(run_id=run_id, episode_id=episode_id, task_id=task_id, split=split)
        self.actor_role = actor_role
        if actor_role:
            self.identity.update(actor_role=actor_role, purpose=purpose or actor_role,
                parent_job_id=parent_job_id or episode_id, role_episode_id=f'{episode_id}:{actor_role}',
                training_target_eligible=actor_role in ('test_case_synthesizer', 'reward_synthesizer'))
        self.retry = retry or RetryConfig()
        self.deadline = deadline or Deadline(None)
        self._in_flight = False

    def events(self, logical):
        return [e for e in self.journal.scan().events if e.episode_id == self.episode_id
                and e.payload.get('logical_call_id') == logical]

    def emit(self, kind, logical, **data):
        return self.journal.append(kind, {**self.identity, 'logical_call_id': logical, **data},
                                   episode_id=self.episode_id, query_id=self.identity['task_id'])

    def call(self, history, *, logical_call_id, **_):
        if self._in_flight:
            raise RuntimeError('one controller request may be in flight per episode')
        self._in_flight = True
        try:
            return self._call(history, logical_call_id)
        finally:
            self._in_flight = False

    def _call(self, history, logical):
        history.assert_prefix_stable()
        events = self.events(logical)
        prepared = next((e.payload for e in events if e.type == 'llm_prepared'), None)
        if prepared:
            request = LLMRequest.model_validate(self.blobs.get_json(prepared['logical_request_ref']))
            if request.expected_history_cursor != history.cursor or request.history_ref != history.history_digest():
                raise StorageError('pending logical request does not match restored history')
            body = self.blobs.get_json(prepared['request_body_ref'])
        else:
            self.deadline.check('llm admission')
            request = LLMRequest(episode_id=self.episode_id, expected_history_cursor=history.cursor,
                prefix_ref=history.prefix_digest(), history_ref=history.history_digest(),
                model_config_ref=digest(self.model_config.model_dump(mode='json')),
                logical_call_id=logical, remaining_budget=self.budget.remaining_all(),
                deadline=self.deadline.absolute_utc, max_output_tokens=self.model_config.max_output_tokens,
                temperature=self.model_config.temperature, messages=history.messages())
            # Freeze AFTER every adapter-added field, BEFORE the transport.
            body = json.loads(json.dumps(self.adapter.build_body(request), allow_nan=False))
            tokens = estimate_tokens(json.dumps(body, ensure_ascii=False), self.model_config.token_counter) + 64
            if tokens + self.model_config.max_output_tokens + self.model_config.context_reserve_tokens > self.model_config.context_limit_tokens:
                raise BudgetExhausted('context budget exhausted', code=Code.CONTEXT_BUDGET_EXHAUSTED)
            if self.actor_role in (None, 'test_case_synthesizer', 'reward_synthesizer'):
                self.budget.debit_once(logical, {'controller_steps': 1.0})
            prepared = dict(request_body_ref=self.blobs.put_json(body),
                logical_request_ref=self.blobs.put_json(request.model_dump(mode='json')),
                request_digest=wire_digest(body), messages_ref=self.blobs.put_json(wire_messages(body)),
                model_config_ref=self.blobs.put_json(self.model_config.model_dump(mode='json')),
                generation_config_ref=self.blobs.put_json({k: v for k, v in body.items() if k != 'messages'}),
                tools_ref=self.blobs.put_json(body.get('tools', [])),
                prefix_digest=request.prefix_ref, history_digest=request.history_ref,
                expected_history_cursor=history.cursor, adapter_version=self.adapter.name,
                step=int(self.budget.episode.consumed['controller_steps']))
            self.emit('llm_prepared', logical, **prepared)
        history.mark_visible()
        last_error = None
        for attempt in range(self.retry.max_transport_attempts):
            pid = f'{logical}:attempt:{attempt}'
            reservation_id = f'{pid}:budget'
            events = self.events(logical)
            these = [e for e in events if e.payload.get('physical_attempt_id') == pid]
            response_event = next((e for e in these if e.type == 'llm_response'), None)
            error_event = next((e for e in these if e.type == 'llm_attempt_error'), None)
            dispatched = any(e.type == 'llm_dispatched' for e in these)
            if response_event:
                data = self.blobs.get_json(response_event.payload['response_ref'])
                response = ProviderResponse(**data)
                self._settle(reservation_id, response)
                return self._result(request, body, pid, response)
            if error_event:
                last_error = error_event.payload
                self.budget.settle(reservation_id, unknown=True)
                if not last_error['retryable']:
                    break
                continue
            if dispatched:
                # Intent survived without a response: execution MAY have happened.
                last_error = dict(code=Code.DISPATCHED_RESULT_UNKNOWN, message='process interrupted after dispatch intent',
                                  retryable=True, dispatched=True, status='unknown')
                self.emit('llm_attempt_error', logical, physical_attempt_id=pid, **last_error)
                self.budget.settle(reservation_id, unknown=True)
                continue  # safe replay only; a new attempt incurs a NEW charge
            self.deadline.check('llm dispatch')
            input_upper = len(json.dumps(body, ensure_ascii=False).encode('utf-8')) + 64
            self.budget.reserve({**({'scoring_requests': 1.0} if self.actor_role == 'rubric_judge' else {}), 'model_requests': 1.0, 'input_tokens': float(input_upper),
                                 'output_tokens': float(request.max_output_tokens)}, operation_id=reservation_id)
            if not any(e.type == 'llm_attempt_prepared' for e in these):
                self.emit('llm_attempt_prepared', logical, physical_attempt_id=pid, attempt=attempt, **prepared)
            fault('llm_before_dispatch')
            self.emit('llm_dispatched', logical, physical_attempt_id=pid)
            fault('llm_after_dispatch')
            started = time.monotonic()
            try:
                with watchdog(self.deadline.bounded(self.model_config.total_timeout_s)):
                    response = self.adapter.send_prepared(body, request, attempt=attempt)
            except (ProviderError, DeadlineExceeded) as exc:
                unknown = getattr(exc, 'dispatched', True)
                last_error = dict(code=exc.code, message=str(exc), retryable=getattr(exc, 'retryable', True),
                    dispatched=unknown, status='unknown' if unknown else 'failed',
                    response_ref=self.blobs.put_json(getattr(exc, 'raw', None)),
                    latency=time.monotonic() - started)
                self.emit('llm_attempt_error', logical, physical_attempt_id=pid, **last_error)
                # Missing usage cannot be treated as a free request, even a 429.
                self.budget.settle(reservation_id, unknown=True)
                if not last_error['retryable']:
                    break
                delay = min(self.retry.backoff_max_s, self.retry.backoff_initial_s * self.retry.backoff_multiplier ** attempt)
                with watchdog(self.deadline.remaining_s()):
                    time.sleep(delay)
                continue
            response_ref = self.blobs.put_json(asdict(response))
            self.emit('llm_response', logical, physical_attempt_id=pid, response_ref=response_ref,
                response_complete=response.finish_reason == 'stop', finish_reason=response.finish_reason,
                provider_request_id=response.provider_request_id, latency=time.monotonic() - started)
            fault('llm_after_response')
            self._settle(reservation_id, response)
            return self._result(request, body, pid, response)
        last_error = last_error or {'code': 'environment_unavailable', 'message': 'retry limit exhausted', 'status': 'failed'}
        return LLMResult(status=last_error['status'], history_cursor=history.cursor, request_digest=wire_digest(body),
            error=StructuredError(category='auth' if last_error['code'] == Code.AUTH_FAILED else 'transport',
                code=last_error['code'], phase='llm_call', retry_owner='none',
                action_outcome=last_error['status'], message=last_error['message']))

    def _settle(self, rid, response):
        if response.usage_known:
            self.budget.settle(rid, {**({'scoring_requests': 1.0} if self.actor_role == 'rubric_judge' else {}), 'model_requests': 1.0, 'input_tokens': float(response.prompt_tokens or 0),
                                     'output_tokens': float(response.completion_tokens or 0)})
        else:
            self.budget.settle(rid, unknown=True)

    def _result(self, request, body, pid, response):
        parsed = (parse_actions(response.text, known_tools=TOOLS, finish_reason=response.finish_reason)
                  if self.actor_role in (None, 'reward_synthesizer') else ParseOutcome())
        return LLMResult(status='incomplete' if response.finish_reason == 'length' else 'complete',
            assistant_text=response.text, proposed_actions=parsed.actions, parse_error=parsed.error,
            request_digest=wire_digest(body), finish_reason=response.finish_reason,
            provider_request_id=response.provider_request_id, history_cursor=request.expected_history_cursor,
            trace_ref=pid)

    def accept(self, history, result, logical, *, schema_valid):
        if result.history_cursor != history.cursor:
            self.emit('llm_late_response', logical, physical_attempt_id=result.trace_ref)
            return False
        self.emit('llm_history_committed', logical, physical_attempt_id=result.trace_ref,
                  schema_valid=schema_valid, response_complete=result.status == 'complete')
        return True
