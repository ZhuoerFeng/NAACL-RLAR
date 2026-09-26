"""Reward-model scoring broker.

The broker lives on the trusted side of the worker boundary. It holds the
credentials, enforces the fixed catalogue, charges the *same* shared budget as
every other request, applies timeouts and bounded retries, and records usage.
The worker only ever gets a score back — never an endpoint, never a key.

Scoring requests are counted separately from controller requests: an RM call is
its own cost and is never folded into "one controller call".
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from ..budget import SharedBudget
from ..config import RMCatalogueEntry, RMConfig
from ..errors import Code
from ..schemas import Usage


@dataclass
class ScoringCallRecord:
    service_request_id: str
    model_id: str
    revision: str
    status: str
    score: float | None
    latency_s: float
    error_code: str | None = None
    usage_unknown: bool = False


@dataclass
class ScoringBroker:
    config: RMConfig
    budget: SharedBudget | None = None
    max_timeout_s: float = 30.0
    #: Injected for tests / offline demos. Signature mirrors an HTTP POST.
    transport: Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]] | None = None
    secret_lookup: Callable[[str], str | None] | None = None
    calls: list[ScoringCallRecord] = field(default_factory=list)
    on_event: Callable[[str, dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        self._catalogue: dict[str, RMCatalogueEntry] = {
            e.model_id: e for e in self.config.catalogue
        }
        self._calls_made = 0

    # -- catalogue ------------------------------------------------------
    def model_cards(self) -> dict[str, dict[str, Any]]:
        """Static catalogue view for the run prefix. No endpoints, no secrets."""
        return {
            e.model_id: {
                "model_id": e.model_id,
                "revision": e.revision,
                "supported_inputs": e.supported_inputs,
                "score_semantics": e.score_semantics,
                "normalization_id": e.normalization_id,
                "model_card_ref": e.model_card_ref,
            }
            for e in self.config.catalogue
        }

    @property
    def usage(self) -> Usage:
        unknown = sum(1 for c in self.calls if c.usage_unknown)
        return Usage(scoring_requests=len(self.calls), unknown_usage_events=unknown)

    # -- request handling -----------------------------------------------
    def handle_scoring_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        import math
        from ..errors import BudgetExhausted
        if payload.get("kind") != "score_model":
            return self._reject(Code.MODEL_NOT_IN_CATALOGUE, "unsupported scoring operation")
        entry = self._catalogue.get(payload.get("model_id"))
        if not self.config.enabled or entry is None:
            return self._reject(Code.MODEL_NOT_IN_CATALOGUE, "model is not in the enabled catalogue")
        body = {"model": entry.model_id, "revision": entry.revision,
                "query": payload.get("query", ""), "response": payload.get("response", ""),
                "metadata": payload.get("allowed_metadata") or {}}
        if body["metadata"] and "metadata" not in entry.supported_inputs:
            return self._reject(Code.FORBIDDEN_API, "catalogue does not permit metadata input")
        headers = {"content-type": "application/json"}
        key = self._secret(entry.api_key_env)
        if entry.api_key_env and not key:
            return self._reject(Code.AUTH_FAILED, "configured scoring credential is unavailable")
        if key:
            headers["authorization"] = f"Bearer {key}"
        import json
        input_upper = len(json.dumps(body, ensure_ascii=False).encode()) + 64
        for attempt in range(entry.max_calls_per_score):
            limit = self.config.max_scoring_requests_per_run
            already = self.budget.run.consumed["scoring_requests"] if self.budget else self._calls_made
            if limit is not None and already >= limit:
                return self._reject(Code.SCORING_BUDGET_EXHAUSTED, "run scoring budget exhausted")
            res = None
            if self.budget:
                try:
                    res = self.budget.reserve({"model_requests": 1.0, "scoring_requests": 1.0,
                        "input_tokens": float(input_upper), "output_tokens": 256.0})
                except BudgetExhausted:
                    return self._reject(Code.SCORING_BUDGET_EXHAUSTED, "shared model/token budget exhausted")
            sid = uuid.uuid4().hex
            started = time.monotonic()
            if self.on_event:
                self.on_event("scoring_dispatched", {"service_request_id": sid, "model_id": entry.model_id,
                    "revision": entry.revision, "attempt": attempt})
            error, retryable, status, data = None, False, "ok", None
            try:
                data = self._post(entry.endpoint_ref, body, headers, min(entry.timeout_s, self.max_timeout_s))
                raw = data["score"]
                if isinstance(raw, bool) or not isinstance(raw, (float, int)) or not math.isfinite(raw):
                    raise ValueError("score must be a finite number")
                score = float(raw)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                error = Code.AUTH_FAILED if status_code in (401, 403) else Code.SCORING_SERVICE_ERROR
                retryable = status_code == 429 or status_code >= 500
                status = "failed"
            except (httpx.HTTPError, TimeoutError):
                error, retryable, status = Code.SCORING_SERVICE_ERROR, True, "unknown"
            except (KeyError, ValueError, TypeError):
                error, status = Code.SCORING_SERVICE_ERROR, "failed"
            self._calls_made += 1
            usage = data.get("usage") if isinstance(data, dict) else None
            known = isinstance(usage, dict) and all(isinstance(usage.get(k), int) and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens"))
            if res is not None:
                self.budget.settle(res, {"model_requests": 1.0, "scoring_requests": 1.0,
                    "input_tokens": float(usage["prompt_tokens"]), "output_tokens": float(usage["completion_tokens"])} if known else None,
                    unknown=not known)
            record = ScoringCallRecord(sid, entry.model_id, entry.revision, status,
                score if not error else None, time.monotonic() - started, error, not known)
            self.calls.append(record)
            self._emit(record)
            if not error:
                return {"status": "ok", "score": score, "service_request_id": sid,
                        "normalization_id": entry.normalization_id}
            if not retryable:
                break
        return {"status": "error", "code": error, "message": "scoring request failed after bounded attempts",
                "service_request_id": sid}

    # -- helpers ---------------------------------------------------------
    def _reject(self, code: str, message: str) -> dict[str, Any]:
        record = ScoringCallRecord(
            service_request_id="",
            model_id="",
            revision="",
            status="rejected",
            score=None,
            latency_s=0.0,
            error_code=code,
        )
        self._emit(record)
        return {"status": "error", "code": code, "message": message}

    def _emit(self, record: ScoringCallRecord) -> None:
        if self.on_event is not None:
            self.on_event(
                "scoring_request",
                {
                    "service_request_id": record.service_request_id,
                    "model_id": record.model_id,
                    "revision": record.revision,
                    "status": record.status,
                    "latency_s": record.latency_s,
                    "error_code": record.error_code,
                    "usage_unknown": record.usage_unknown,
                },
            )

    def _secret(self, env_name: str | None) -> str | None:
        if not env_name:
            return None
        if self.secret_lookup is not None:
            return self.secret_lookup(env_name)
        import os

        return os.environ.get(env_name)

    def _post(
        self, url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        if self.transport is not None:
            return self.transport(url, body, headers, timeout)
        import asyncio
        async def send():
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await asyncio.wait_for(client.post(url, json=body, headers=headers), timeout)
                response.raise_for_status()
                return response.json()
        return asyncio.run(send())
