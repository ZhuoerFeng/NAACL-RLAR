"""Journal-backed accounting and bounded, explicitly injected crash points."""
from __future__ import annotations

import os
import signal
import threading
from contextlib import contextmanager

from .budget import BudgetLedger, SharedBudget
from .errors import DeadlineExceeded, StorageError


def fault(point: str) -> None:
    # Only an explicit test environment variable enables this hook. SIGKILL is
    # intentional: exception injection does not test filesystem commit order.
    if os.environ.get("RLAR_TEST_KILL_AT") == point:
        os.kill(os.getpid(), signal.SIGKILL)


@contextmanager
def watchdog(seconds: float | None):
    """Interrupt blocking network/I/O in the main thread, not just admission."""
    if seconds is None or threading.current_thread() is not threading.main_thread():
        yield
        return
    if seconds <= 0:
        raise DeadlineExceeded("absolute wall deadline reached")
    old_handler = signal.getsignal(signal.SIGALRM)
    previous = signal.getitimer(signal.ITIMER_REAL)
    import time
    started = time.monotonic()

    def expire(*_):
        raise DeadlineExceeded("active operation exceeded its wall deadline")

    signal.signal(signal.SIGALRM, expire)
    signal.setitimer(signal.ITIMER_REAL, min(seconds, previous[0]) if previous[0] else seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)
        if previous[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, previous[0] - (time.monotonic() - started)))


class PersistentBudget(SharedBudget):
    """One durable snapshot per atomic reserve/settle, shared by all workers.

    Named reservations make logical admissions idempotent across a restart.
    Unresolved physical attempts keep their entire reserved upper bound.
    """
    def __init__(self, run, episode, journal, episode_id, events=()):
        super().__init__(run, episode)
        self.journal, self.episode_id = journal, episode_id
        self.operations = {}
        self.lock = threading.RLock()
        for e in events:
            if e.type == "budget_snapshot" and e.episode_id == episode_id:
                self.run = BudgetLedger.from_json(e.payload["run"])
                self.episode = BudgetLedger.from_json(e.payload["episode"])
                self.operations = e.payload["operations"]

    def _save(self):
        self.journal.append("budget_snapshot", {
            "run": self.run.to_json(), "episode": self.episode.to_json(),
            "operations": self.operations,
        }, episode_id=self.episode_id)

    def reserve(self, amounts, *, operation_id=None):
        import uuid
        with self.lock:
            key = operation_id or uuid.uuid4().hex
            if key in self.operations:
                if self.operations[key]["amounts"] != amounts:
                    raise StorageError("reservation input changed on resume")
                return key
            reservations = super().reserve(amounts)
            self.operations[key] = {"amounts": amounts, "settled": False,
                "ids": [r.reservation_id for r in reservations]}
            self._save()
            return key

    def settle(self, reservations, actual=None, *, unknown=False):
        with self.lock:
            op = self.operations[reservations]
            if op["settled"]:
                return
            for ledger, rid in zip(self.ledgers, op["ids"]):
                ledger.settle(ledger._open[rid], actual, unknown=unknown)
            op["settled"] = True
            op["unknown"] = unknown
            self._save()

    def charge(self, amounts):
        res = self.reserve(amounts)
        self.settle(res)

    def debit_once(self, key, amounts):
        res = self.reserve(amounts, operation_id=key)
        self.settle(res)

    def usage(self):
        from .schemas import Usage
        c = {k: self.episode.consumed[k] + self.episode.reserved[k] for k in self.episode.consumed}
        return Usage(controller_logical_calls=int(c["controller_steps"]),
            physical_requests=int(c["model_requests"] - c["scoring_requests"]),
            scoring_requests=int(c["scoring_requests"]), tool_calls=int(c["tool_calls"]),
            test_cases=int(c["test_cases"]), component_executions=int(c["component_executions"]),
            input_tokens=int(c["input_tokens"]), output_tokens=int(c["output_tokens"]),
            unknown_usage_events=sum(bool(v.get("unknown")) or not v["settled"] for v in self.operations.values()))
