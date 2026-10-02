"""Trusted-side program execution behind ``context.run_code``.

A reward component never imports the packages a task needs. It composes a
program and asks the trusted parent to run it in a declared, version-pinned
interpreter; the structured outcome comes back over the worker pipe. A failing
program (non-zero exit, exception, timeout) is an observation the component
scores itself; only an unusable environment is a scoring-service error.

Same prototype scope as ``SubprocessRunner``: a fresh process per call, an
empty temporary directory, a scrubbed environment, wall/CPU/file-size limits
and process-group cleanup. It is not a filesystem or network boundary.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from ..storage.canonical import digest

ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ")
IS_POSIX = os.name == "posix"

_PROBE = r'''
import json, sys
from importlib import metadata
names = json.loads(sys.argv[1])
out = {"python": "%d.%d.%d" % sys.version_info[:3], "packages": {}}
for name in names:
    try:
        out["packages"][name] = metadata.version(name)
    except metadata.PackageNotFoundError:
        out["packages"][name] = None
print(json.dumps(out))
'''


def probe_environment(python, spec):
    """Problems that make the declared environment unusable (empty when usable)."""
    if not Path(python).exists():
        return [f"code environment interpreter does not exist: {python}"]
    try:
        proc = subprocess.run([python, "-I", "-c", _PROBE, json.dumps(sorted(spec.packages))],
                              capture_output=True, text=True, timeout=60, env=_scrubbed_env(tempfile.gettempdir()))
        found = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return [f"code environment {spec.env_id!r} could not be probed: {exc}"]
    return [f"code environment {spec.env_id!r}: {name} is {found['packages'].get(name)!r}, task pack pins {want!r}"
            for name, want in sorted(spec.packages.items()) if found["packages"].get(name) != want]


def _scrubbed_env(work_dir):
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    # Deliberately absent: every API key and secret in the parent environment.
    env.update(HOME=str(work_dir), TMPDIR=str(work_dir), PYTHONHASHSEED="0",
               PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    return env


class CodeExecutor:
    """Runs one program per call; at most ``max_calls_per_example`` per bound execution."""

    def __init__(self, python, spec, *, recorder=None):
        self.python, self.spec, self.recorder = python, spec, recorder
        self.active = None
        self.calls = 0
        self._usable = None

    def bind_execution(self, request, component):
        # SubprocessRunner executes one example per request, so this is per example.
        self.active = (request.request_id + ':' + digest(request.example_ids), component)
        self.calls = 0

    def handle(self, payload):
        if self.active is None:
            return {'status': 'error', 'code': 'forbidden_api', 'message': 'run_code outside a bound execution'}
        request_id, component = self.active
        if 'run_code' not in component.required_apis:
            return {'status': 'error', 'code': 'forbidden_api', 'message': 'run_code is not declared by this component'}
        if self.calls >= self.spec.max_calls_per_example:
            return {'status': 'error', 'code': 'forbidden_api',
                    'message': f'run_code allows {self.spec.max_calls_per_example} call(s) per component/example'}
        source, stdin = payload.get('source'), payload.get('stdin', '')
        if not isinstance(source, str) or not isinstance(stdin, str) or len(source) + len(stdin) > self.spec.max_source_chars:
            return {'status': 'error', 'code': 'forbidden_api', 'message': 'run_code source/stdin must be bounded text'}
        timeout = payload.get('timeout_s')
        timeout = self.spec.timeout_s if timeout is None else timeout
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= self.spec.timeout_s:
            return {'status': 'error', 'code': 'forbidden_api',
                    'message': f'timeout_s must be in (0, {self.spec.timeout_s}]'}
        if self._usable is None:
            self._usable = not probe_environment(self.python, self.spec)
        if not self._usable:
            return {'status': 'error', 'code': 'scoring_service_error',
                    'message': f'code environment {self.spec.env_id!r} is unavailable or does not match its pinned packages'}
        self.calls += 1
        call_id = f'{request_id}:run_code:{component.id}:{self.calls}'
        try:
            outcome = self.run(source, stdin, float(timeout))
        except OSError as exc:
            return {'status': 'error', 'code': 'scoring_service_error', 'message': f'code execution failed to start: {exc}'}
        if self.recorder is not None:
            self.recorder(call_id, {'source': source, 'stdin': stdin, 'timeout_s': timeout,
                                    'env_id': self.spec.env_id, 'component_id': component.id}, outcome)
        return {'status': 'ok', 'result': outcome, 'service_request_id': call_id}

    def run(self, source, stdin, timeout):
        limit = self.spec.max_output_bytes
        with tempfile.TemporaryDirectory(prefix='run-code-') as tmp:
            # Captured output lives beside, not inside, the program's directory.
            work = Path(tmp) / 'work'
            work.mkdir()
            (work / 'main.py').write_text(source, encoding='utf-8')
            out_path, err_path = Path(tmp) / 'stdout.log', Path(tmp) / 'stderr.log'
            started = time.monotonic()
            status = None
            with open(out_path, 'wb') as out, open(err_path, 'wb') as err:
                proc = subprocess.Popen([self.python, '-I', 'main.py'], cwd=work, env=_scrubbed_env(work),  # noqa: S603
                                        stdin=subprocess.PIPE, stdout=out, stderr=err,
                                        start_new_session=IS_POSIX,
                                        preexec_fn=self._limits(timeout) if IS_POSIX else None)
                try:
                    proc.communicate(stdin.encode('utf-8'), timeout=timeout)
                except subprocess.TimeoutExpired:
                    status = 'timeout'
                finally:
                    _kill_group(proc)
            wall = time.monotonic() - started
            stdout, out_cut = _read(out_path, limit)
            stderr, err_cut = _read(err_path, limit)
        code = proc.returncode
        if status is None and code is not None and code < 0:
            sig = -code
            status = ('output_limit' if sig == getattr(signal, 'SIGXFSZ', None)
                      else 'timeout' if sig in (getattr(signal, 'SIGXCPU', None), signal.SIGKILL) else 'error')
        if status is None and code != 0 and (out_cut or err_cut):
            # Python ignores SIGXFSZ, so the file-size limit surfaces as a failed write.
            status = 'output_limit'
        if status is None:
            status = 'ok' if code == 0 else 'error'
        return {'status': status, 'exit_code': code if code is not None and code >= 0 else None,
                'stdout': stdout, 'stderr': stderr, 'stdout_truncated': out_cut, 'stderr_truncated': err_cut,
                'wall_seconds': round(wall, 4)}

    def _limits(self, timeout):
        output, memory = self.spec.max_output_bytes, self.spec.memory_limit_mb

        def apply():
            import resource
            cpu = max(1, int(timeout) + 1)
            for name, value in (('RLIMIT_CPU', cpu), ('RLIMIT_FSIZE', output * 2 + 4096)):
                try:
                    resource.setrlimit(getattr(resource, name), (value, value))
                except (ValueError, OSError):
                    pass
            if memory and sys.platform.startswith('linux'):
                # RLIMIT_AS is unreliable on macOS; enforced on Linux only.
                try:
                    resource.setrlimit(resource.RLIMIT_AS, (memory << 20, memory << 20))
                except (ValueError, OSError):
                    pass
        return apply


class ScoringRouter:
    """One worker pipe, several trusted services selected by request kind."""

    def __init__(self, judge=None, code=None):
        self.judge, self.code = judge, code
        self.main_thread_dispatch = bool(getattr(judge, 'main_thread_dispatch', False))

    def bind_execution(self, request, component):
        for service in (self.judge, self.code):
            if service is not None and hasattr(service, 'bind_execution'):
                service.bind_execution(request, component)

    def handle_scoring_request(self, payload):
        kind = payload.get('kind')
        if kind == 'run_code':
            if self.code is None:
                return {'status': 'error', 'code': 'forbidden_api', 'message': 'no code environment is declared'}
            return self.code.handle(payload)
        if self.judge is None:
            return {'status': 'error', 'code': 'scoring_service_error', 'message': 'no judge utility was configured'}
        return self.judge.handle_scoring_request(payload)


def _read(path, limit):
    size = path.stat().st_size if path.exists() else 0
    with open(path, 'rb') as fh:
        data = fh.read(limit)
    return data.decode('utf-8', errors='replace'), size > limit


def _kill_group(proc):
    if proc.poll() is None or IS_POSIX:
        try:
            os.killpg(proc.pid, signal.SIGKILL) if IS_POSIX else proc.kill()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover
        pass
