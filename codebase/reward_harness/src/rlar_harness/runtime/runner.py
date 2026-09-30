"""Runner interface and the controlled-prototype subprocess backend.

The driver never imports or ``exec``s generated code. It hands an immutable
:class:`~rlar_harness.schemas.ExecuteRequest` to a :class:`Runner`, which is
free to be a local subprocess today and a remote isolated service later
without any protocol change.

**Honest scope of ``SubprocessRunner``.** It is a *controlled prototype*. It
gives you: process-level crash containment, a wall-clock timeout, CPU-time and
file-size rlimits, a scrubbed environment, a private working directory, and
process-group cleanup that also reaps children the reward code spawned. It does
**not** give you a security boundary: the worker runs as the same OS user and
can read the same files and reach the same network. Anything that depends on a
real boundary — formal audit, running arbitrary untrusted model-generated code
— must use a runner whose ``capabilities()`` actually declare isolation.
Capabilities the platform cannot enforce are reported as ``False``; they are
never reported as enforced.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Protocol

from ..errors import ActionOutcome, Code, ErrorCategory, RetryOwner
from ..schemas import (
    ComponentResult,
    ExecuteRequest,
    ExecuteResponse,
    RunnerCapabilities,
    StructuredError,
    Usage,
)

IS_POSIX = os.name == "posix"


class ScoringService(Protocol):
    """Trusted-side handler for ``context.score_model`` requests."""

    def handle_scoring_request(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class Runner(ABC):
    @abstractmethod
    def capabilities(self) -> RunnerCapabilities: ...

    @abstractmethod
    def execute(self, request: ExecuteRequest) -> ExecuteResponse: ...

    @abstractmethod
    def cancel(self, request_id: str) -> bool: ...

    def lookup(self, request_id: str) -> ExecuteResponse | None:
        """Remote runners override this. Local execution has nothing to look up."""
        return None


def _runtime_error(
    code: str,
    message: str,
    *,
    category: ErrorCategory = ErrorCategory.RESOURCE,
    owner: RetryOwner = RetryOwner.AGENT,
    outcome: ActionOutcome = ActionOutcome.FAILED,
    detail: str | None = None,
) -> StructuredError:
    return StructuredError(
        category=category,
        code=code,
        phase="execution",
        retry_owner=owner,
        action_outcome=outcome,
        message=message,
        detail_ref=None,
        suggested_recovery=detail,
    )


class SubprocessRunner(Runner):
    """One worker subprocess per component; components never share a process."""

    def __init__(
        self,
        *,
        scoring_service: ScoringService | None = None,
        python_executable: str | None = None,
        env_allowlist: tuple[str, ...] = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"),
        memory_limit_mb: int | None = None,
    ) -> None:
        self.scoring_service = scoring_service
        self.python = python_executable or sys.executable
        self.env_allowlist = env_allowlist
        self.memory_limit_mb = memory_limit_mb
        self.work_root = None
        self._active: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    # -- capabilities ---------------------------------------------------
    def capabilities(self) -> RunnerCapabilities:
        linux = sys.platform.startswith("linux")
        return RunnerCapabilities(
            runner_type="subprocess",
            filesystem_isolation=False,
            network_isolation=False,
            label_secret_isolation=False,
            # RLIMIT_AS is only dependable on Linux; saying otherwise would be
            # reporting a limit we do not actually apply.
            memory_limit_enforced=bool(linux and self.memory_limit_mb),
            process_limit_enforced=False,
            cpu_time_limit_enforced=IS_POSIX,
            wall_timeout_enforced=True,
            process_group_cleanup=IS_POSIX,
            max_assurance="behavioral_prototype",
            platform=f"{platform.system()} {platform.release()} ({platform.machine()})",
            notes=[
                "same-OS-user process: not a filesystem or network security boundary",
                "audit labels held by this user are reachable from the worker",
                "memory limits are enforced on Linux only" if not linux else "",
                "spawned child processes are reaped via process-group kill"
                if IS_POSIX
                else "no process-group cleanup on this platform",
            ],
        )

    # -- execution ------------------------------------------------------
    def execute(self, request: ExecuteRequest) -> ExecuteResponse:
        started = time.monotonic()
        # A new interpreter for EVERY example resets module globals and imports.
        if len(request.examples) > 1:
            responses = [self.execute(request.model_copy(update={"examples": [example],
                         "example_ids": [eid]})) for example, eid in zip(request.examples, request.example_ids)]
            usage = Usage()
            for response in responses:
                usage = usage.merged(response.usage)
            return ExecuteResponse(request_id=request.request_id, example_ids=request.example_ids,
                per_example=[r.per_example[0] for r in responses], status="completed", usage=usage,
                service_request_ids=[sid for r in responses for sid in r.service_request_ids])
        # per_component[component_index][example_index]
        per_component: list[list[ComponentResult]] = []
        service_request_ids: list[str] = []
        usage = Usage()

        for component in request.definition.components:
            results, ids, comp_usage = self._run_one_component(request, component)
            per_component.append(results)
            service_request_ids.extend(ids)
            usage = usage.merged(comp_usage)

        # Transpose to per-example order.
        per_example: list[list[ComponentResult]] = []
        for example_index in range(len(request.example_ids)):
            per_example.append([col[example_index] for col in per_component])

        usage = usage.merged(Usage(wall_seconds=time.monotonic() - started))
        return ExecuteResponse(
            request_id=request.request_id,
            per_example=per_example,
            example_ids=list(request.example_ids),
            status="completed",
            usage=usage,
            service_request_ids=service_request_ids,
        )

    def cancel(self, request_id: str) -> bool:
        with self._lock:
            proc = self._active.get(request_id)
        if proc is None:
            return False
        _kill_tree(proc)
        return True

    # -- internals ------------------------------------------------------
    def _run_one_component(
        self, request: ExecuteRequest, component
    ) -> tuple[list[ComponentResult], list[str], Usage]:
        n = len(request.example_ids)
        undeclared = [a for a in component.required_apis if a not in request.permitted_apis]
        if undeclared:
            err = _runtime_error(
                Code.FORBIDDEN_API,
                f"component {component.id!r} requires APIs not permitted by this "
                f"profile: {undeclared}",
                category=ErrorCategory.REWARD_CODE,
            )
            return (
                [
                    ComponentResult(id=component.id, status="error", error=err)
                    for _ in range(n)
                ],
                [],
                Usage(component_executions=n),
            )

        work_dir = Path(tempfile.mkdtemp(prefix="worker-", dir=self.work_root))
        req_path = work_dir / "request.json"
        resp_path = work_dir / "response.json"
        out_path = work_dir / "stdout.log"
        err_path = work_dir / "stderr.log"

        payload = {
            "parent_pid": os.getpid(),
            "component": component.model_dump(mode="json"),
            "examples": request.examples,
            "example_ids": list(request.example_ids),
            "permitted_apis": list(request.permitted_apis),
            "reward_logic_policy": request.definition.runtime_contract.reward_logic_policy,
            "dependencies": request.definition.runtime_contract.dependencies,
            "cpu_timeout_s": request.cpu_timeout_s,
            "memory_limit_mb": request.memory_limit_mb,
            "max_output_bytes": request.max_output_bytes,
            "max_return_bytes": request.max_return_bytes,
        }
        req_path.write_text(json.dumps(payload), encoding="utf-8")

        env = {k: v for k, v in os.environ.items() if k in self.env_allowlist}
        env["HOME"] = str(work_dir)
        env["TMPDIR"] = str(work_dir)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        # Deliberately absent: every API key and secret in the parent env.

        scoring_ids: list[str] = []
        pipes: list[int] = []
        pass_fds: tuple[int, ...] = ()
        servicer: _ScoringServicer | None = None
        if self.scoring_service is not None:
            req_r, req_w = os.pipe()
            resp_r, resp_w = os.pipe()
            pipes = [req_r, req_w, resp_r, resp_w]
            env["RLAR_SCORING_REQ_FD"] = str(req_w)
            env["RLAR_SCORING_RESP_FD"] = str(resp_r)
            pass_fds = (req_w, resp_r)
            if hasattr(self.scoring_service, "max_timeout_s"):
                self.scoring_service.max_timeout_s = request.wall_timeout_s
            if hasattr(self.scoring_service, 'bind_execution'):
                self.scoring_service.bind_execution(request, component)
            servicer = _ScoringServicer(req_r, resp_w, self.scoring_service)

        cmd = [
            self.python,
            "-I",  # isolated mode: ignore PYTHON* env and the user site dir
            "-m",
            "rlar_harness.runtime.worker",
            "--request",
            str(req_path),
            "--response",
            str(resp_path),
        ]

        status_code: str | None = None
        detail = ""
        proc: subprocess.Popen | None = None
        try:
            with open(out_path, "wb") as out_fh, open(err_path, "wb") as err_fh:
                proc = subprocess.Popen(  # noqa: S603
                    cmd,
                    cwd=str(work_dir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out_fh,
                    stderr=err_fh,
                    pass_fds=pass_fds,
                    start_new_session=IS_POSIX,  # own process group -> killable as a tree
                )
            with self._lock:
                self._active[request.request_id] = proc
            for fd in pass_fds:
                os.close(fd)
                pipes.remove(fd)
            if servicer is not None:
                servicer.start()
            try:
                if servicer is not None and getattr(self.scoring_service, 'main_thread_dispatch', False):
                    until = time.monotonic() + request.wall_timeout_s
                    while proc.poll() is None:
                        if time.monotonic() >= until:
                            raise subprocess.TimeoutExpired(cmd, request.wall_timeout_s)
                        servicer.pump()
                        try:
                            proc.wait(timeout=min(0.02, max(0.001, until - time.monotonic())))
                        except subprocess.TimeoutExpired:
                            pass
                else:
                    proc.wait(timeout=request.wall_timeout_s)
            except subprocess.TimeoutExpired:
                _kill_tree(proc)
                status_code = Code.COMPONENT_TIMEOUT
                detail = f"component exceeded the {request.wall_timeout_s}s wall limit"
        except BaseException:
            _cleanup_dir(work_dir)
            raise
        finally:
            if proc is not None:
                _kill_tree(proc)
            if servicer is not None:
                servicer.stop()
                scoring_ids = servicer.request_ids
            for fd in pipes:
                try:
                    os.close(fd)
                except OSError:
                    pass
            with self._lock:
                self._active.pop(request.request_id, None)

        if servicer is not None and servicer.failure is not None:
            _cleanup_dir(work_dir)
            raise servicer.failure

        returncode = proc.returncode if proc is not None else None
        if status_code is None and returncode is not None and returncode < 0:
            sig = -returncode
            if sig == getattr(signal, "SIGXFSZ", None):
                status_code = Code.COMPONENT_OUTPUT_LIMIT
                detail = "component exceeded the output size limit (SIGXFSZ)"
            elif sig == getattr(signal, "SIGXCPU", None):
                status_code = Code.COMPONENT_TIMEOUT
                detail = "component exceeded the CPU time limit (SIGXCPU)"
            elif sig == signal.SIGKILL:
                status_code = Code.COMPONENT_TIMEOUT
                detail = "component was killed (wall or resource limit)"
            else:
                status_code = Code.COMPONENT_RAISED
                detail = f"worker terminated by signal {sig}"

        stdout_bytes, stdout_truncated = _read_bounded(out_path, request.max_output_bytes)
        stderr_bytes, stderr_truncated = _read_bounded(err_path, request.max_output_bytes)
        truncated = stdout_truncated or stderr_truncated

        results: list[ComponentResult]
        if status_code is not None:
            owner = (
                RetryOwner.AGENT
                if status_code
                in (Code.COMPONENT_TIMEOUT, Code.COMPONENT_OUTPUT_LIMIT)
                else RetryOwner.HARNESS
            )
            err = _runtime_error(
                status_code,
                detail,
                category=ErrorCategory.TIMEOUT
                if status_code == Code.COMPONENT_TIMEOUT
                else ErrorCategory.RESOURCE,
                owner=owner,
                detail=_tail(stderr_bytes),
            )
            results = [
                ComponentResult(
                    id=component.id,
                    status="error",
                    error=err,
                    stdout_truncated=truncated,
                )
                for _ in range(n)
            ]
        else:
            results = self._parse_response(
                resp_path, component.id, request.example_ids, truncated, _tail(stderr_bytes)
            )

        _cleanup_dir(work_dir)
        return results, scoring_ids, Usage(component_executions=n, scoring_requests=len(scoring_ids))

    def _parse_response(
        self,
        resp_path: Path,
        component_id: str,
        example_ids: list[str],
        truncated: bool,
        stderr_tail: str,
    ) -> list[ComponentResult]:
        n = len(example_ids)
        if not resp_path.exists():
            err = _runtime_error(
                Code.WORKER_UNAVAILABLE,
                "worker exited without writing a structured result",
                owner=RetryOwner.HARNESS,
                detail=stderr_tail,
            )
            return [
                ComponentResult(id=component_id, status="error", error=err) for _ in range(n)
            ]
        try:
            payload = json.loads(resp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            err = _runtime_error(
                Code.WORKER_UNAVAILABLE,
                f"worker result was unreadable: {exc}",
                owner=RetryOwner.HARNESS,
                detail=stderr_tail,
            )
            return [
                ComponentResult(id=component_id, status="error", error=err) for _ in range(n)
            ]

        if payload.get("status") != "completed":
            werr = payload.get("error") or {}
            err = _runtime_error(
                werr.get("code", Code.WORKER_UNAVAILABLE),
                werr.get("message", "worker reported a failure"),
                owner=RetryOwner.HARNESS,
                detail=werr.get("detail"),
            )
            return [
                ComponentResult(id=component_id, status="error", error=err) for _ in range(n)
            ]

        by_example = {r["example_id"]: r for r in payload.get("results", [])}
        out: list[ComponentResult] = []
        for example_id in example_ids:
            raw = by_example.get(example_id)
            if raw is None:
                out.append(
                    ComponentResult(
                        id=component_id,
                        status="error",
                        error=_runtime_error(
                            Code.WORKER_UNAVAILABLE,
                            f"worker returned no result for example {example_id}",
                            owner=RetryOwner.HARNESS,
                        ),
                    )
                )
                continue
            if raw.get("status") == "error":
                werr = raw.get("error") or {}
                code = werr.get("code", Code.COMPONENT_RAISED)
                out.append(
                    ComponentResult(
                        id=component_id,
                        status="error",
                        stdout_truncated=truncated,
                        error=_runtime_error(
                            code,
                            werr.get("message", ""),
                            category=(
                                ErrorCategory.RESOURCE
                                if code == Code.SCORING_SERVICE_ERROR
                                else ErrorCategory.REWARD_CODE
                            ),
                            owner=(
                                RetryOwner.HARNESS
                                if code == Code.SCORING_SERVICE_ERROR
                                else RetryOwner.AGENT
                            ),
                            detail=werr.get("detail"),
                        ),
                    )
                )
                continue
            # Raw value validation and normalization are the trusted side's job
            # (see runtime/aggregate.py). Pass it through untouched here; the
            # runner deliberately does not decide whether it is an acceptable
            # score.
            out.append(
                ComponentResult(
                    id=component_id,
                    status="ok",
                    raw_value=raw.get("raw_value"),
                    stdout_truncated=truncated,
                )
            )
        return out


class _ScoringServicer(threading.Thread):
    """Services ``score_model`` requests from a worker on the trusted side."""

    def __init__(self, req_fd: int, resp_fd: int, service: ScoringService) -> None:
        super().__init__(daemon=True)
        self._req_fd = req_fd
        self._resp_fd = resp_fd
        self._service = service
        self.request_ids: list[str] = []
        self.failure: BaseException | None = None
        self._stop_event = threading.Event()
        import queue
        self.pending = queue.Queue()

    def pump(self):
        import queue
        try:
            item = self.pending.get_nowait()
        except queue.Empty:
            return
        try:
            item['response'] = self._service.handle_scoring_request(item['payload'])
        finally:
            item['done'].set()

    def run(self) -> None:
        try:
            with os.fdopen(self._req_fd, "r", encoding="utf-8") as reader, os.fdopen(
                self._resp_fd, "w", encoding="utf-8"
            ) as writer:
                for line in reader:
                    if self._stop_event.is_set():
                        break
                    try:
                        payload = json.loads(line)
                        if getattr(self._service, 'main_thread_dispatch', False):
                            item = {'payload': payload, 'done': threading.Event()}
                            self.pending.put(item)
                            while not item['done'].wait(0.02):
                                if self._stop_event.is_set():
                                    return
                            if 'response' not in item:
                                return
                            response = item['response']
                        else:
                            response = self._service.handle_scoring_request(payload)
                    except BaseException as exc:
                        self.failure = exc
                        break
                    if response.get("service_request_id"):
                        self.request_ids.append(response["service_request_id"])
                    writer.write(json.dumps(response) + "\n")
                    writer.flush()
        except (OSError, ValueError):
            pass

    def stop(self) -> None:
        self._stop_event.set()
        self.join(timeout=2.0)


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the worker and everything it spawned."""
    if IS_POSIX:
        try:
            # The leader may already have exited while a child remains alive.
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                pass
    else:  # pragma: no cover - Windows has no process groups here
        try:
            proc.kill()
        except OSError:
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass


def _read_bounded(path: Path, limit: int) -> tuple[bytes, bool]:
    if not path.exists():
        return b"", False
    size = path.stat().st_size
    with open(path, "rb") as fh:
        data = fh.read(limit)
    return data, size > limit


def _tail(data: bytes, limit: int = 2000) -> str:
    text = data.decode("utf-8", errors="replace")
    return text[-limit:] if len(text) > limit else text


def _cleanup_dir(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def make_execute_request(
    *,
    definition,
    examples: list[dict[str, Any]],
    example_ids: list[str],
    permitted_apis: list[str],
    runtime_fingerprint: str,
    wall_timeout_s: float,
    cpu_timeout_s: float,
    memory_limit_mb: int | None,
    max_output_bytes: int,
    max_return_bytes: int,
    action_id: str,
) -> ExecuteRequest:
    return ExecuteRequest(
        request_id=action_id,
        action_id=action_id,
        definition=definition,
        examples=examples,
        example_ids=example_ids,
        runtime_fingerprint=runtime_fingerprint,
        permitted_apis=permitted_apis,
        wall_timeout_s=wall_timeout_s,
        cpu_timeout_s=cpu_timeout_s,
        memory_limit_mb=memory_limit_mb,
        max_output_bytes=max_output_bytes,
        max_return_bytes=max_return_bytes,
    )
