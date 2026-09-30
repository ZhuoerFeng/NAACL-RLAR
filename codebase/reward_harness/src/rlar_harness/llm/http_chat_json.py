"""``http_chat_json_v1`` — the Chat Completions wire dialect.

Request body::

    {"model": ..., "messages": [{"role": ..., "content": ...}],
     "temperature": ..., "max_tokens": ..., "stream": false}

Response fields read::

    choices[0].message.content      -> assistant text
    choices[0].finish_reason        -> "stop" | "length" | ...
    id                              -> provider request id
    usage.prompt_tokens             -> input tokens
    usage.completion_tokens         -> output tokens

Anything else in the response is ignored. ``finish_reason == "length"`` marks
the turn incomplete and no action batch is executed from it. A response with no
``usage`` object records an *unknown usage event* rather than a zero, so cost
accounting never silently under-reports.

The API key is read from the configured environment variable at send time. It
goes into the ``authorization`` header only; it is never placed in the body,
never included in the request digest, and never written to a trace.

Local HTTP fixtures verify this contract. Real-service evidence and its scope
are recorded separately in the delivery report and per-run artifacts.
"""

from __future__ import annotations

import os
from typing import Any, Callable

import httpx

from ..config import ModelConfig
from ..errors import Code
from ..schemas import LLMRequest
from .adapter import LLMAdapter, ProviderError, ProviderResponse

ADAPTER_VERSION = "http_chat_json_v1"

#: Header names that must never appear in a trace or a request digest.
REDACTED_HEADERS = frozenset({"authorization", "api-key", "x-api-key", "cookie"})


class HttpChatJsonAdapter(LLMAdapter):
    name = ADAPTER_VERSION

    def __init__(
        self,
        config: ModelConfig,
        *,
        client: httpx.Client | None = None,
        secret_lookup: Callable[[str], str | None] | None = None,
    ) -> None:
        if not config.endpoint:
            raise ValueError("http_chat_json_v1 requires an endpoint")
        self.config = config
        self._secret_lookup = secret_lookup or os.environ.get
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=config.connect_timeout_s,
                read=config.read_timeout_s,
                write=config.read_timeout_s,
                pool=config.connect_timeout_s,
            )
        )

    # -- wire ------------------------------------------------------------
    def build_body(self, request: LLMRequest) -> dict[str, Any]:
        body = {
            "model": self.config.model,
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
            self.config.max_tokens_field: request.max_output_tokens,
            "stream": False,
        }
        if self.config.send_temperature:
            body["temperature"] = request.temperature
        for name in ("reasoning_effort", "enable_thinking", "top_p", "prompt_cache_key"):
            value = getattr(self.config, name)
            if value is not None:
                body[name] = value
        if self.config.response_format:
            body["response_format"] = {"type": self.config.response_format}
        return body

    def build_headers(self) -> dict[str, str]:
        headers = {"content-type": "application/json"}
        if self.config.api_key_env:
            key = self._secret_lookup(self.config.api_key_env)
            if not key:
                raise ProviderError(
                    f"environment variable {self.config.api_key_env!r} is not set; "
                    "no credential is available for this endpoint",
                    code=Code.AUTH_FAILED,
                    retryable=False,
                    dispatched=False,
                )
            headers["authorization"] = f"Bearer {key}"
        return headers

    def send(self, request: LLMRequest, *, attempt: int) -> ProviderResponse:
        return self.send_prepared(self.build_body(request), request, attempt=attempt)

    def send_prepared(self, body: dict, request: LLMRequest, *, attempt: int) -> ProviderResponse:
        headers = self.build_headers()
        try:
            response = self._client.post(
                self.config.endpoint,
                json=body,
                headers=headers,
                timeout=httpx.Timeout(connect=self.config.connect_timeout_s, read=self.config.read_timeout_s,
                                      write=self.config.read_timeout_s, pool=self.config.connect_timeout_s),
            )
        except httpx.ConnectError as exc:
            # No connection was established, so nothing was processed.
            raise ProviderError(
                f"connect failed: {exc}",
                code=Code.CONNECTION_LOST,
                retryable=True,
                dispatched=False,
            ) from exc
        except httpx.ConnectTimeout as exc:
            raise ProviderError(
                f"connect timed out: {exc}",
                code=Code.CONNECTION_LOST,
                retryable=True,
                dispatched=False,
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            # The request was on the wire. Whether the provider ran it is
            # unknowable from this side, so the outcome stays unknown.
            raise ProviderError(
                f"timed out waiting for the response: {exc}",
                code=Code.DISPATCHED_RESULT_UNKNOWN,
                retryable=True,
                dispatched=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(
                f"transport error: {type(exc).__name__}: {exc}",
                code=Code.CONNECTION_LOST,
                retryable=True,
                dispatched=True,
            ) from exc

        raw = {"status_code": response.status_code, "text": response.text}
        try:
            result = self.parse_response(response)
        except ProviderError as exc:
            exc.raw = raw
            raise
        result.raw = raw
        return result

    def response_object(self, response: httpx.Response) -> dict:
        status = response.status_code
        if status in (401, 403):
            raise ProviderError(
                f"authentication rejected with HTTP {status}",
                code=Code.AUTH_FAILED,
                retryable=False,
                dispatched=True,
                status_code=status,
            )
        if status == 429:
            raise ProviderError(
                "rate limited",
                code=Code.HTTP_RATE_LIMITED,
                retryable=True,
                dispatched=False,
                status_code=status,
                retry_after_s=_retry_after(response),
            )
        if 500 <= status < 600:
            raise ProviderError(
                f"server error HTTP {status}",
                code=Code.HTTP_SERVER_ERROR,
                retryable=True,
                dispatched=True,
                status_code=status,
                retry_after_s=_retry_after(response),
            )
        if status >= 400:
            # 4xx other than 429/401/403 is a request the harness built wrong.
            # Retrying the identical bytes cannot fix it.
            raise ProviderError(
                f"request rejected with HTTP {status}: {_snippet(response)}",
                code=Code.HTTP_BAD_RESPONSE,
                retryable=False,
                dispatched=True,
                status_code=status,
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderError(
                f"response body was not JSON: {_snippet(response)}",
                code=Code.HTTP_BAD_RESPONSE,
                retryable=True,
                dispatched=True,
                status_code=status,
            ) from exc

        if not isinstance(data, dict):
            raise ProviderError(
                f"response JSON was {type(data).__name__}, expected an object",
                code=Code.HTTP_BAD_RESPONSE,
                retryable=True,
                dispatched=True,
                status_code=status,
            )

        return data

    def parse_response(self, response: httpx.Response) -> ProviderResponse:
        data = self.response_object(response)
        status = response.status_code
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ProviderError(
                "response has no 'choices' array",
                code=Code.HTTP_BAD_RESPONSE,
                retryable=True,
                dispatched=True,
                status_code=status,
            )
        choice = choices[0]
        if not isinstance(choice, dict):
            raise ProviderError(
                "choices[0] is not an object",
                code=Code.HTTP_BAD_RESPONSE,
                retryable=True,
                dispatched=True,
                status_code=status,
            )
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(
            message.get("content"), str
        ):
            raise ProviderError(
                "choices[0].message.content is missing or not a string",
                code=Code.HTTP_BAD_RESPONSE,
                retryable=True,
                dispatched=True,
                status_code=status,
            )

        usage = data.get("usage")
        prompt_tokens = completion_tokens = None
        usage_known = False
        if isinstance(usage, dict):
            prompt_tokens = _as_int(usage.get("prompt_tokens"))
            completion_tokens = _as_int(usage.get("completion_tokens"))
            usage_known = prompt_tokens is not None and completion_tokens is not None

        finish_reason = choice.get("finish_reason")
        if message.get("refusal") or finish_reason not in ("stop", "length"):
            raise ProviderError("refused or unsupported finish reason", code=Code.HTTP_BAD_RESPONSE, dispatched=True)
        return ProviderResponse(
            text=message["content"],
            finish_reason=finish_reason if isinstance(finish_reason, str) else None,
            provider_request_id=data.get("id") if isinstance(data.get("id"), str) else None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            usage_known=usage_known,
            cached_tokens=_as_int((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) if isinstance(usage, dict) else None,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 and value == int(value) else None


def _snippet(response: httpx.Response, limit: int = 200) -> str:
    try:
        text = response.text
    except Exception:  # noqa: BLE001 - diagnostics must not raise
        return "<unreadable body>"
    return text[:limit]


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    """Header view safe to write into a trace."""
    return {
        k: ("<redacted>" if k.lower() in REDACTED_HEADERS else v)
        for k, v in headers.items()
    }
