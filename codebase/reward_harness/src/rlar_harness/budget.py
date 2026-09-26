"""Shared budget ledger with persisted reservations, plus deadline handling.

Rules that the rest of the harness depends on:

* Run and episode ledgers are debited by the *same* events. A checklist
  component execution, a controller transport retry and an RM sub-request all
  come out of the same upper budget; per-dimension counters are additionally
  kept for reporting.
* Reservations are persisted *before* dispatch and settled after. If the actual
  usage comes back unknown, the reservation is kept and flagged unknown — it is
  never zeroed.
* Recovery, worker replacement and process restarts re-attach to the same
  ledger. Restarting an episode does not mint new allowance.
* Deadlines are stored as absolute UTC but enforced with a monotonic clock, so
  a machine that was asleep for an hour does not get an extra hour.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .errors import BudgetExhausted, DeadlineExceeded

#: Every budget dimension the harness tracks. ``None`` in ``limits`` means the
#: dimension is uncapped, which must be an explicit configuration choice.
DIMENSIONS = (
    "controller_steps",
    "revisions",
    "tool_calls",
    "test_cases",
    "component_executions",
    "model_requests",  # physical LLM requests, including transport retries
    "scoring_requests",  # RM / judge requests made from inside a reward
    "input_tokens",
    "output_tokens",
)


@dataclass
class Reservation:
    reservation_id: str
    amounts: dict[str, float]
    settled: bool = False
    unknown: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "amounts": dict(self.amounts),
            "settled": self.settled,
            "unknown": self.unknown,
        }


@dataclass
class BudgetLedger:
    """One level (run or episode) of the shared ledger."""

    name: str
    limits: dict[str, float | None] = field(default_factory=dict)
    consumed: dict[str, float] = field(default_factory=dict)
    reserved: dict[str, float] = field(default_factory=dict)
    unknown_reservations: dict[str, float] = field(default_factory=dict)
    _open: dict[str, Reservation] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for dim in DIMENSIONS:
            self.limits.setdefault(dim, None)
            self.consumed.setdefault(dim, 0.0)
            self.reserved.setdefault(dim, 0.0)
            self.unknown_reservations.setdefault(dim, 0.0)

    # -- queries --------------------------------------------------------
    def remaining(self, dim: str) -> float | None:
        limit = self.limits.get(dim)
        if limit is None:
            return None
        return limit - self.consumed.get(dim, 0.0) - self.reserved.get(dim, 0.0)

    def remaining_all(self) -> dict[str, float | None]:
        return {dim: self.remaining(dim) for dim in DIMENSIONS}

    def would_exceed(self, amounts: dict[str, float]) -> list[str]:
        blocked = []
        for dim, amount in amounts.items():
            rem = self.remaining(dim)
            if rem is not None and amount > rem:
                blocked.append(dim)
        return blocked

    # -- mutation -------------------------------------------------------
    def reserve(self, amounts: dict[str, float], *, reservation_id: str | None = None) -> Reservation:
        blocked = self.would_exceed(amounts)
        if blocked:
            raise BudgetExhausted(
                f"{self.name} budget exhausted for {sorted(blocked)}; "
                f"remaining={ {d: self.remaining(d) for d in blocked} }"
            )
        res = Reservation(reservation_id or uuid.uuid4().hex, dict(amounts))
        for dim, amount in amounts.items():
            self.reserved[dim] = self.reserved.get(dim, 0.0) + amount
        self._open[res.reservation_id] = res
        return res

    def settle(
        self,
        reservation: Reservation,
        actual: dict[str, float] | None = None,
        *,
        unknown: bool = False,
    ) -> None:
        """Release a reservation and record what was actually consumed.

        ``unknown=True`` keeps the reserved amount charged and flags it, which
        is what happens when a dispatched request's outcome cannot be confirmed.
        """
        if reservation.settled:
            return
        reservation.settled = True
        reservation.unknown = unknown
        self._open.pop(reservation.reservation_id, None)
        for dim, amount in reservation.amounts.items():
            self.reserved[dim] = max(0.0, self.reserved.get(dim, 0.0) - amount)
        if unknown:
            for dim, amount in reservation.amounts.items():
                self.consumed[dim] = self.consumed.get(dim, 0.0) + amount
                self.unknown_reservations[dim] = (
                    self.unknown_reservations.get(dim, 0.0) + amount
                )
            return
        charged = actual if actual is not None else reservation.amounts
        for dim, amount in charged.items():
            self.consumed[dim] = self.consumed.get(dim, 0.0) + amount

    def charge(self, amounts: dict[str, float]) -> None:
        """Charge usage that was not pre-reserved (e.g. reported token counts)."""
        for dim, amount in amounts.items():
            self.consumed[dim] = self.consumed.get(dim, 0.0) + amount

    # -- persistence ----------------------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "limits": dict(self.limits),
            "consumed": dict(self.consumed),
            "reserved": dict(self.reserved),
            "unknown_reservations": dict(self.unknown_reservations),
            "open_reservations": [r.to_json() for r in self._open.values()],
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> "BudgetLedger":
        ledger = BudgetLedger(
            name=data.get("name", "ledger"),
            limits=dict(data.get("limits") or {}),
            consumed=dict(data.get("consumed") or {}),
            reserved=dict(data.get("reserved") or {}),
            unknown_reservations=dict(data.get("unknown_reservations") or {}),
        )
        for entry in data.get("open_reservations") or []:
            res = Reservation(entry["reservation_id"], dict(entry["amounts"]))
            ledger._open[res.reservation_id] = res
        return ledger


class SharedBudget:
    """Run-level and episode-level ledgers charged together."""

    def __init__(self, run: BudgetLedger, episode: BudgetLedger | None = None) -> None:
        self.run = run
        self.episode = episode

    @property
    def ledgers(self) -> list[BudgetLedger]:
        return [l for l in (self.run, self.episode) if l is not None]

    def remaining_all(self) -> dict[str, float | None]:
        out: dict[str, float | None] = {}
        for dim in DIMENSIONS:
            values = [l.remaining(dim) for l in self.ledgers]
            finite = [v for v in values if v is not None]
            out[dim] = min(finite) if finite else None
        return out

    def reserve(self, amounts: dict[str, float]) -> list[Reservation]:
        taken: list[Reservation] = []
        try:
            for ledger in self.ledgers:
                taken.append(ledger.reserve(amounts))
        except BudgetExhausted:
            for ledger, res in zip(self.ledgers, taken):
                ledger.settle(res, {dim: 0.0 for dim in res.amounts})
            raise
        return taken

    def settle(
        self,
        reservations: list[Reservation],
        actual: dict[str, float] | None = None,
        *,
        unknown: bool = False,
    ) -> None:
        for ledger, res in zip(self.ledgers, reservations):
            ledger.settle(res, actual, unknown=unknown)

    def charge(self, amounts: dict[str, float]) -> None:
        for ledger in self.ledgers:
            ledger.charge(amounts)


class Deadline:
    """Absolute UTC deadline enforced through a monotonic watchdog."""

    def __init__(self, absolute_utc: float | None, *, now_utc: float | None = None) -> None:
        self.absolute_utc = absolute_utc
        if absolute_utc is None:
            self._monotonic_deadline: float | None = None
            self.expired_on_attach = False
            return
        now = time.time() if now_utc is None else now_utc
        remaining = absolute_utc - now
        self.expired_on_attach = remaining <= 0
        self._monotonic_deadline = time.monotonic() + max(0.0, remaining)

    @property
    def enabled(self) -> bool:
        return self._monotonic_deadline is not None

    def remaining_s(self) -> float | None:
        if self._monotonic_deadline is None:
            return None
        return self._monotonic_deadline - time.monotonic()

    def expired(self) -> bool:
        if self._monotonic_deadline is None:
            return False
        return self.remaining_s() <= 0

    def check(self, phase: str = "run") -> None:
        if self.expired():
            raise DeadlineExceeded(f"{phase}: absolute wall deadline reached")

    def bounded(self, requested_s: float) -> float:
        """Clamp a per-call timeout to what remains of the episode deadline."""
        rem = self.remaining_s()
        if rem is None:
            return requested_s
        return max(0.0, min(requested_s, rem))


class StagnationTracker:
    """Detects repeated identical failures / no-progress loops."""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold
        self._last_signature: str | None = None
        self._repeats = 0

    def observe(self, signature: str) -> bool:
        """Record an outcome signature. Returns True when the limit is reached."""
        if signature == self._last_signature:
            self._repeats += 1
        else:
            self._last_signature = signature
            self._repeats = 1
        return self._repeats >= self.threshold

    @property
    def repeats(self) -> int:
        return self._repeats
