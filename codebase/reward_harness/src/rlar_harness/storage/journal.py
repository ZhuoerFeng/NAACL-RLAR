"""Append-only event journal (``trace.jsonl``).

Every event carries a unique ``event_id`` and a strictly increasing ``seq``.
Recovery replays the journal, de-duplicating by ``event_id``.

Damage handling is deliberately asymmetric, because "best effort skipping" is
how a crashed run silently loses a commit:

* An **incomplete trailing record** (the process died mid-write, so the file
  does not end with a newline) is treated as never having been written. It is
  truncated on recovery.
* A **corrupt record in the middle**, a **seq regression**, or a **reference to
  a blob that does not exist** stops recovery with an error. The run is not
  resumed until an operator looks at it.
"""

from __future__ import annotations

import json
import threading
import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..errors import JournalCorruptError
from ..schemas import EVENT_SCHEMA_VERSION
from .blobs import BlobStore, is_blob_ref
from .canonical import canonical_json

JOURNAL_NAME = "trace.jsonl"


class EventType:
    RUN_STARTED = "run_started"
    RUN_STOPPED = "run_stopped"
    RUN_RESUMED = "run_resumed"
    INPUT_RECORD_READ = "input_record_read"
    INPUT_RECORD_REJECTED = "input_record_rejected"
    EPISODE_STARTED = "episode_started"
    EPISODE_STATE = "episode_state"
    REUSE_CHECKED = "reuse_checked"
    PREFIX_FROZEN = "prefix_frozen"
    BUDGET_RESERVED = "budget_reserved"
    BUDGET_SETTLED = "budget_settled"
    LLM_REQUEST_INTENT = "llm_request_intent"
    LLM_PHYSICAL_ATTEMPT = "llm_physical_attempt"
    LLM_RESULT = "llm_result"
    MESSAGE_COMMITTED = "message_committed"
    ACTION_INTENT = "action_intent"
    ACTION_DISPATCHED = "action_dispatched"
    ACTION_RAW_RESULT = "action_raw_result"
    OBSERVATION_COMMITTED = "observation_committed"
    VALIDATION_REPORT = "validation_report"
    SCORING_REQUEST = "scoring_request"
    LIBRARY_APPENDED = "library_appended"
    RESULT_COMMITTED = "result_committed"
    CHECKPOINT_WRITTEN = "checkpoint_written"
    EPISODE_FINISHED = "episode_finished"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass
class Event:
    event_id: str
    seq: int
    ts: float
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    episode_id: str | None = None
    query_id: str | None = None
    schema_version: str = EVENT_SCHEMA_VERSION

    def to_json(self) -> str:
        return canonical_json(
            {
                "schema_version": self.schema_version,
                "event_id": self.event_id,
                "seq": self.seq,
                "ts": self.ts,
                "type": self.type,
                "episode_id": self.episode_id,
                "query_id": self.query_id,
                "payload": self.payload,
            }
        ).decode("utf-8")

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "Event":
        return Event(
            event_id=data["event_id"],
            seq=int(data["seq"]),
            ts=float(data["ts"]),
            type=data["type"],
            payload=data.get("payload") or {},
            episode_id=data.get("episode_id"),
            query_id=data.get("query_id"),
            schema_version=data.get("schema_version", EVENT_SCHEMA_VERSION),
        )


@dataclass
class JournalScan:
    events: list[Event]
    truncated_tail_bytes: int
    next_seq: int


class Journal:
    """Single-writer append-only journal."""

    def __init__(self, path: Path, blobs: BlobStore, *, clock=None) -> None:
        self.path = Path(path)
        self.blobs = blobs
        self._seq = 0
        self._lock = threading.RLock()
        self._clock = clock
        self._seen_event_ids: set[str] = set()
        self._fh = None

    # -- clock ---------------------------------------------------------
    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        import time

        return time.time()

    # -- write ---------------------------------------------------------
    def append(self, *args, **kwargs):
        with self._lock:
            event_id = kwargs.get("event_id")
            if event_id and event_id in self._seen_event_ids:
                return next(e for e in self.scan().events if e.event_id == event_id)
            return self._append(*args, **kwargs)

    def _append(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        *,
        episode_id: str | None = None,
        query_id: str | None = None,
        event_id: str | None = None,
    ) -> Event:
        self._seq += 1
        event = Event(
            event_id=event_id or uuid.uuid4().hex,
            seq=self._seq,
            ts=self._now(),
            type=type,
            payload=payload or {},
            episode_id=episode_id,
            query_id=query_id,
        )
        self._write(event)
        self._seen_event_ids.add(event.event_id)
        return event

    def _write(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(event.to_json() + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    @property
    def seq(self) -> int:
        return self._seq

    # -- read / recover -------------------------------------------------
    def scan(self, *, verify_blobs: bool = True, repair_tail: bool = False) -> JournalScan:
        with self._lock:
            return self._scan(verify_blobs=verify_blobs, repair_tail=repair_tail)

    def _scan(self, *, verify_blobs: bool = True, repair_tail: bool = False) -> JournalScan:
        if not self.path.exists():
            return JournalScan(events=[], truncated_tail_bytes=0, next_seq=0)

        raw = self.path.read_bytes()
        truncated = 0
        if raw and not raw.endswith(b"\n"):
            # The final record was never durably completed.
            last_nl = raw.rfind(b"\n")
            cut = last_nl + 1
            truncated = len(raw) - cut
            if repair_tail:
                with open(self.path, "r+b") as fh:
                    fh.truncate(cut)
                    fh.flush()
                    os.fsync(fh.fileno())
            raw = raw[:cut]

        events: list[Event] = []
        seen: set[str] = set()
        last_seq = 0
        for lineno, line in enumerate(raw.decode("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                event = Event.from_dict(data)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise JournalCorruptError(
                    f"{self.path}: record at line {lineno} is corrupt and is not the "
                    f"incomplete final record; refusing to skip it ({exc})"
                ) from exc
            if event.seq <= last_seq:
                raise JournalCorruptError(
                    f"{self.path}: seq regression at line {lineno} "
                    f"({event.seq} after {last_seq})"
                )
            last_seq = event.seq
            if event.event_id in seen:
                continue  # idempotent replay of an already-recorded event
            seen.add(event.event_id)
            if verify_blobs:
                self._verify_refs(event, lineno)
            events.append(event)

        self._seq = last_seq
        self._seen_event_ids = seen
        return JournalScan(
            events=events, truncated_tail_bytes=truncated, next_seq=last_seq + 1
        )

    def _verify_refs(self, event: Event, lineno: int) -> None:
        for ref in _iter_blob_refs(event.payload):
            self.blobs.get_bytes(ref)
            if not self.blobs.exists(ref):
                raise JournalCorruptError(
                    f"{self.path}: event at line {lineno} ({event.type}) references "
                    f"missing blob {ref}"
                )

    def iter_events(self, **kwargs) -> Iterator[Event]:
        yield from self.scan(**kwargs).events


def _iter_blob_refs(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        if is_blob_ref(value):
            yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _iter_blob_refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from _iter_blob_refs(v)
