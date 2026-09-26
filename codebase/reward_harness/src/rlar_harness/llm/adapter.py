"""Provider adapter interface.

An adapter turns a materialized message list into one physical request and
returns a normalized response. It owns the wire dialect and nothing else: no
retries, no budget, no history, no parsing of the action envelope.

The one thing every adapter must get right is ``dispatched``. When a request
fails, the client has to know whether the server could have processed it. A
connect-timeout on a connection that was never established is ``failed``; a
read-timeout after the bytes went out is ``unknown``, and the harness treats it
as possibly-completed for the rest of the episode.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..errors import Code
from ..schemas import LLMRequest


@dataclass
class ProviderResponse:
    text: str
    finish_reason: str | None = None
    provider_request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    #: False when the provider reported no usage at all. The harness records the
    #: gap as an ``unknown_usage_event`` instead of writing a zero.
    usage_known: bool = True
    cost_usd: float | None = None
    cached_tokens: int | None = None
    raw: object = None


class ProviderError(Exception):
    """A physical request failed.

    ``dispatched`` is the load-bearing field: ``False`` means the request
    provably never reached the provider and may be re-sent freely; ``True``
    means it may have been processed and the outcome is unknown.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str = Code.HTTP_BAD_RESPONSE,
        retryable: bool = False,
        dispatched: bool = False,
        status_code: int | None = None,
        retry_after_s: float | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.retryable = retryable
        self.dispatched = dispatched
        self.status_code = status_code
        self.retry_after_s = retry_after_s


class LLMAdapter(ABC):
    """One physical request in, one normalized response out."""

    name: str = "adapter"

    @abstractmethod
    def send(self, request: LLMRequest, *, attempt: int) -> ProviderResponse:
        """Perform exactly one physical request. Must not retry internally."""

    def build_body(self, request: LLMRequest) -> dict:
        return {"messages": [{"role": m.role, "content": m.content} for m in request.messages],
                "temperature": request.temperature, "max_tokens": request.max_output_tokens,
                "stream": False, "model": self.name}

    def send_prepared(self, body: dict, request: LLMRequest, *, attempt: int) -> ProviderResponse:
        return self.send(request, attempt=attempt)

    def count_tokens(self, text: str) -> int | None:
        """Adapter-provided token count, when the provider exposes one."""
        return None

    def close(self) -> None:  # pragma: no cover - default is a no-op
        return None
