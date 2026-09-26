"""Scripted provider adapter.

``ScriptedLLM`` replays a fixed list of turns and can inject faults before any
of them. It exists so the full controller loop — prefix, batching, tool
dispatch, validation, submission, recovery — can be exercised end to end with
no network and no paid API, while still driving the *real* parsing, the *real*
subprocess execution and the *real* trusted evaluator.

Two properties matter for the acceptance tests:

* every physical attempt is recorded, so a test can assert that a transport
  retry re-sent the same ``request_digest`` and did not consume a new turn;
* faults are described declaratively (``rate_limited``, ``server_error``,
  ``read_timeout_after_dispatch``, ``auth``), including the one fault whose
  outcome is genuinely unknown.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable

from ..errors import Code
from ..schemas import LLMRequest
from .adapter import LLMAdapter, ProviderError, ProviderResponse


@dataclass
class Turn:
    """One scripted assistant response."""

    content: str
    finish_reason: str | None = "stop"
    prompt_tokens: int | None = 100
    completion_tokens: int | None = 20
    usage_known: bool = True
    #: Faults raised *before* this turn is returned, one per physical attempt.
    #: The turn itself is only consumed once the fault list is drained.
    faults: list[str] = field(default_factory=list)

    @staticmethod
    def actions(*actions: dict[str, Any], **kwargs: Any) -> "Turn":
        import json

        return Turn(content=json.dumps({"actions": list(actions)}), **kwargs)


_FAULTS: dict[str, dict[str, Any]] = {
    "rate_limited": {
        "code": Code.HTTP_RATE_LIMITED,
        "retryable": True,
        "dispatched": False,
        "status_code": 429,
    },
    "server_error": {
        "code": Code.HTTP_SERVER_ERROR,
        "retryable": True,
        "dispatched": False,
        "status_code": 503,
    },
    "connection_lost": {
        "code": Code.CONNECTION_LOST,
        "retryable": True,
        "dispatched": False,
    },
    # The important one: bytes went out, the response never came back. Whether
    # the provider processed it is genuinely unknowable from here.
    "read_timeout_after_dispatch": {
        "code": Code.DISPATCHED_RESULT_UNKNOWN,
        "retryable": True,
        "dispatched": True,
    },
    "auth": {
        "code": Code.AUTH_FAILED,
        "retryable": False,
        "dispatched": False,
        "status_code": 401,
    },
    "bad_response": {
        "code": Code.HTTP_BAD_RESPONSE,
        "retryable": False,
        "dispatched": True,
    },
}


@dataclass
class Attempt:
    logical_call_id: str
    request_digest: str
    turn_index: int
    outcome: str
    history_length: int


class ScriptedLLM(LLMAdapter):
    name = "scripted"

    def __init__(
        self,
        turns: list[Turn] | None = None,
        *,
        on_exhausted: Callable[[LLMRequest], Turn] | None = None,
    ) -> None:
        self.turns = list(turns or [])
        self.on_exhausted = on_exhausted
        self.attempts: list[Attempt] = []
        self.requests: list[LLMRequest] = []
        self._cursor = 0
        self._ids = itertools.count(1)

    # -- introspection used by tests --------------------------------------
    @property
    def physical_attempts(self) -> int:
        return len(self.attempts)

    @property
    def turns_consumed(self) -> int:
        return self._cursor

    def digests(self) -> list[str]:
        return [a.request_digest for a in self.attempts]

    # -- adapter ----------------------------------------------------------
    def send(self, request: LLMRequest, *, attempt: int) -> ProviderResponse:
        from .client import request_digest

        digest = request_digest(request)
        if self._cursor >= len(self.turns):
            if self.on_exhausted is None:
                self.attempts.append(
                    Attempt(
                        request.logical_call_id, digest, self._cursor, "exhausted",
                        len(request.messages),
                    )
                )
                raise ProviderError(
                    f"scripted LLM has no turn {self._cursor}; the controller "
                    "made more calls than the script provides",
                    code=Code.HTTP_BAD_RESPONSE,
                    retryable=False,
                )
            turn = self.on_exhausted(request)
        else:
            turn = self.turns[self._cursor]

        if turn.faults:
            fault = turn.faults.pop(0)
            spec = _FAULTS.get(fault)
            if spec is None:
                raise ValueError(f"unknown scripted fault {fault!r}")
            self.attempts.append(
                Attempt(
                    request.logical_call_id, digest, self._cursor, f"fault:{fault}",
                    len(request.messages),
                )
            )
            raise ProviderError(f"scripted fault {fault!r}", **spec)

        self.requests.append(request)
        self.attempts.append(
            Attempt(
                request.logical_call_id, digest, self._cursor, "ok",
                len(request.messages),
            )
        )
        self._cursor += 1
        return ProviderResponse(
            text=turn.content,
            finish_reason=turn.finish_reason,
            provider_request_id=f"scripted-{next(self._ids)}",
            prompt_tokens=turn.prompt_tokens,
            completion_tokens=turn.completion_tokens,
            usage_known=turn.usage_known,
        )
