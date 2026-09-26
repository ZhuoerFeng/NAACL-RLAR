"""Execution worker: loads one reward component and scores examples with it.

Run as ``python -m rlar_harness.runtime.worker --request X --response Y``.

One worker handles exactly one component, so a syntax error, an import error,
a crash or a hang in one component cannot affect the others. Structured results
go to the response file; anything the component prints goes to the process's
stdout/stderr, which the parent captures separately and bounds.

The worker never sees oracle labels, API keys or the dataset. Reward model
scoring is proxied back to the trusted parent over a dedicated pipe.

This file must stay importable with the standard library plus
``rlar_harness.evaluation.checkers``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from typing import Any

from ..evaluation.checkers import BUNDLE_METHODS, BUNDLES, CheckerError, ForbiddenAPI


class ScoringUnavailable(Exception):
    """Preserve causal error codes across the worker IPC boundary."""
    def __init__(self, message, code="scoring_service_error"):
        super().__init__(message)
        self.code = code


class ScoringProxy:
    """Sends ``score_model`` requests back to the parent over a pipe pair."""

    def __init__(self, req_fd: int, resp_fd: int) -> None:
        self._req = os.fdopen(req_fd, "w", encoding="utf-8")
        self._resp = os.fdopen(resp_fd, "r", encoding="utf-8")
        self.request_ids: list[str] = []

    def call(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._req.write(json.dumps(payload) + "\n")
        self._req.flush()
        line = self._resp.readline()
        if not line:
            raise ScoringUnavailable("scoring channel closed by the harness")
        response = json.loads(line)
        if response.get("service_request_id"):
            self.request_ids.append(response["service_request_id"])
        return response


class ScoringContext:
    """The only capability surface reward code gets.

    Attribute access is restricted to methods of the bundles this profile
    permits; everything else raises :class:`ForbiddenAPI`.
    """

    def __init__(
        self,
        permitted_apis: list[str],
        required_apis: list[str],
        proxy: ScoringProxy | None,
        component_id: str,
    ) -> None:
        self._permitted = set(permitted_apis)
        self._required = set(required_apis)
        self._proxy = proxy
        self._component_id = component_id
        self._method_owner: dict[str, str] = {}
        self._instances: dict[str, Any] = {}
        for api_id in self._permitted:
            cls = BUNDLES.get(api_id)
            if cls is None:
                continue
            self._instances[api_id] = cls()
            for method in BUNDLE_METHODS.get(api_id, ()):
                self._method_owner[method] = api_id

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        owner = self._method_owner.get(name)
        if owner is None:
            raise ForbiddenAPI(
                f"context.{name} is not available to this profile; "
                f"permitted APIs: {sorted(self._permitted)}"
            )
        if owner not in self._required:
            raise ForbiddenAPI(
                f"context.{name} belongs to API {owner!r}, which component "
                f"{self._component_id!r} did not declare in required_apis"
            )
        return getattr(self._instances[owner], name)

    def score_model(
        self,
        model_id: str,
        query: str,
        response: str,
        allowed_metadata: dict[str, Any] | None = None,
    ) -> float:
        """Request a reward-model score through the trusted-side broker."""
        if "reward_model_scoring_v1" not in self._permitted:
            raise ForbiddenAPI("reward model scoring is not permitted by this profile")
        if "reward_model_scoring_v1" not in self._required:
            raise ForbiddenAPI(
                "component did not declare 'reward_model_scoring_v1' in required_apis"
            )
        if self._proxy is None:
            raise ScoringUnavailable("no scoring channel was provided to this worker")
        result = self._proxy.call(
            {
                "kind": "score_model",
                "model_id": model_id,
                "query": query,
                "response": response,
                "allowed_metadata": allowed_metadata or {},
            }
        )
        if result.get("status") != "ok":
            raise ScoringUnavailable(
                f"{result.get('message', '')}", code=result.get("code", "scoring_service_error")
            )
        return float(result["score"])


def _error(code: str, message: str, *, detail: str | None = None) -> dict[str, Any]:
    return {"code": code, "message": message, "detail": detail}


def run_component(request: dict[str, Any], proxy: ScoringProxy | None) -> list[dict[str, Any]]:
    component = request["component"]
    component_id = component["id"]
    source = component["source"]
    entrypoint = component.get("entrypoint", "score")
    permitted_apis: list[str] = request.get("permitted_apis", [])
    required_apis: list[str] = component.get("required_apis", [])
    examples: list[dict[str, Any]] = request["examples"]
    example_ids: list[str] = request["example_ids"]

    # -- load the component once; a load failure fails every example, but only
    #    for this component.
    load_error: dict[str, Any] | None = None
    fn = None
    namespace: dict[str, Any] = {"__name__": f"rlar_component_{component_id}"}
    try:
        compiled = compile(source, f"<component:{component_id}>", "exec")
    except SyntaxError as exc:
        load_error = _error(
            "component_syntax_error",
            f"{exc.msg} (line {exc.lineno})",
            detail="".join(traceback.format_exception_only(type(exc), exc)),
        )
    else:
        try:
            exec(compiled, namespace)  # noqa: S102 - this is the controlled prototype worker
        except ImportError as exc:
            load_error = _error(
                "component_import_error", str(exc), detail=traceback.format_exc()
            )
        except BaseException as exc:  # noqa: BLE001 - report anything the module does
            load_error = _error(
                "component_import_error",
                f"{type(exc).__name__}: {exc}",
                detail=traceback.format_exc(),
            )
        else:
            fn = namespace.get(entrypoint)
            if not callable(fn):
                load_error = _error(
                    "component_missing_entrypoint",
                    f"module does not define a callable {entrypoint!r}",
                )

    results: list[dict[str, Any]] = []
    for example_id, example in zip(example_ids, examples):
        if load_error is not None:
            results.append(
                {"example_id": example_id, "status": "error", "error": load_error}
            )
            continue
        # Fresh context per example: no state leaks between examples.
        context = ScoringContext(permitted_apis, required_apis, proxy, component_id)
        try:
            value = fn(example, context)
        except ForbiddenAPI as exc:
            results.append(
                {
                    "example_id": example_id,
                    "status": "error",
                    "error": _error("forbidden_api", str(exc)),
                }
            )
        except ScoringUnavailable as exc:
            results.append(
                {
                    "example_id": example_id,
                    "status": "error",
                    "error": _error(exc.code, str(exc)),
                }
            )
        except OSError as exc:
            # A component that floods stdout trips RLIMIT_FSIZE, which surfaces
            # here as EFBIG. Report it as an output-limit breach rather than a
            # generic exception so the diagnostic is actionable.
            code = (
                "component_output_limit"
                if exc.errno == getattr(__import__("errno"), "EFBIG", None)
                else "component_raised"
            )
            results.append(
                {
                    "example_id": example_id,
                    "status": "error",
                    "error": _error(code, f"{type(exc).__name__}: {exc}"),
                }
            )
        except CheckerError as exc:
            results.append(
                {
                    "example_id": example_id,
                    "status": "error",
                    "error": _error("component_raised", f"CheckerError: {exc}"),
                }
            )
        except BaseException as exc:  # noqa: BLE001
            results.append(
                {
                    "example_id": example_id,
                    "status": "error",
                    "error": _error(
                        "component_raised",
                        f"{type(exc).__name__}: {exc}",
                        detail=traceback.format_exc(limit=6),
                    ),
                }
            )
        else:
            # The raw value is passed through as JSON so the trusted parent can
            # tell True from 1 and reject it if the ABI says so. Normalization,
            # range checks and aggregation all happen on the trusted side.
            results.append(
                {"example_id": example_id, "status": "returned", "raw_value": _jsonable(value)}
            )
    return results


def _jsonable(value: Any) -> Any:
    """Represent the component's return value without losing its type."""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if value != value:
            return {"__nonfinite__": "nan"}
        if value == float("inf"):
            return {"__nonfinite__": "inf"}
        if value == float("-inf"):
            return {"__nonfinite__": "-inf"}
        return value
    return {"__unsupported_type__": type(value).__name__}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rlar-worker")
    parser.add_argument("--request", required=True)
    parser.add_argument("--response", required=True)
    args = parser.parse_args(argv)

    with open(args.request, encoding="utf-8") as fh:
        request = json.load(fh)

    _apply_limits(request)
    # The driver may be SIGKILLed and cannot run finally. This bounded prototype
    # watchdog kills the controlled worker group when its recorded parent dies.
    if os.name == "posix" and request.get("parent_pid"):
        import threading, time, signal
        parent_pid = request["parent_pid"]
        def parent_watchdog():
            while True:
                time.sleep(0.1)
                if os.getppid() != parent_pid:
                    os.killpg(os.getpgrp(), signal.SIGKILL)
        threading.Thread(target=parent_watchdog, daemon=True).start()


    proxy = None
    req_fd = os.environ.get("RLAR_SCORING_REQ_FD")
    resp_fd = os.environ.get("RLAR_SCORING_RESP_FD")
    if req_fd and resp_fd:
        proxy = ScoringProxy(int(req_fd), int(resp_fd))

    try:
        results = run_component(request, proxy)
        payload = {
            "status": "completed",
            "results": results,
            "service_request_ids": proxy.request_ids if proxy else [],
        }
    except BaseException as exc:  # noqa: BLE001
        payload = {
            "status": "failed",
            "results": [],
            "error": {
                "code": "worker_internal_error",
                "message": f"{type(exc).__name__}: {exc}",
                "detail": traceback.format_exc(),
            },
        }

    data = json.dumps(payload)
    max_return = int(request.get("max_return_bytes", 262144))
    encoded = data.encode("utf-8")
    if len(encoded) > max_return:
        payload = {
            "status": "failed",
            "results": [],
            "error": {
                "code": "component_output_limit",
                "message": f"structured result exceeded {max_return} bytes",
                "detail": None,
            },
        }
        encoded = json.dumps(payload).encode("utf-8")
    with open(args.response, "wb") as fh:
        fh.write(encoded)
        fh.flush()
        os.fsync(fh.fileno())
    return 0


def _apply_limits(request: dict[str, Any]) -> None:
    """Best-effort resource limits. What cannot be enforced is declared, not faked."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return
    cpu = request.get("cpu_timeout_s")
    if cpu:
        seconds = max(1, int(cpu) + 1)
        try:
            resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds))
        except (ValueError, OSError):
            pass
    fsize = int(request.get("max_output_bytes", 65536)) + int(
        request.get("max_return_bytes", 262144)
    )
    try:
        resource.setrlimit(resource.RLIMIT_FSIZE, (fsize, fsize))
    except (ValueError, OSError):
        pass
    mem_mb = request.get("memory_limit_mb")
    if mem_mb and sys.platform.startswith("linux"):
        # RLIMIT_AS is not reliably enforced on macOS, so it is only applied
        # where it works and the capability report says so.
        nbytes = int(mem_mb) * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_AS, (nbytes, nbytes))
        except (ValueError, OSError):
            pass


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    raise SystemExit(main())
