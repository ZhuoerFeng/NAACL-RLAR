"""Atomically-replaced run/episode checkpoints.

A checkpoint is an optimization: everything in it can be rebuilt from
``results.jsonl`` plus ``trace.jsonl``. It must never be the only record of a
commit, which is why :meth:`CheckpointStore.write` happens *after* the result
row is durable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from ..schemas import CHECKPOINT_SCHEMA_VERSION, EpisodeState, Strict, Usage

CHECKPOINT_DIR = "checkpoints"
LATEST_NAME = "latest.json"


class PendingAction(Strict):
    action_id: str
    tool: str
    arguments_digest: str
    #: Position in the assistant's declared batch order. Observations are handed
    #: back to the model in this order regardless of completion time.
    batch_index: int
    dispatched: bool = False
    outcome: Literal["pending", "completed", "failed", "unknown"] = "pending"
    external_request_id: str | None = None
    result_ref: str | None = None
    reserved_budget: dict[str, Any] = Field(default_factory=dict)
    #: Whether this action may be replayed if its outcome is unknown.
    safe_to_replay: bool = True


class EpisodeCheckpoint(Strict):
    episode_id: str
    query_id: str
    job_key: str
    attempt_id: int
    state: EpisodeState
    input_digest: str
    config_digest: str
    library_snapshot: str
    prefix_digest: str
    history_digest: str
    history_cursor: int
    history_refs: list[str] = Field(default_factory=list)
    adapter_version: str = ""
    logical_call_id: str | None = None
    request_digest: str | None = None
    pending_batch: list[PendingAction] = Field(default_factory=list)
    current_reward_key: str | None = None
    eligible_candidates: list[dict[str, Any]] = Field(default_factory=list)
    consumed_budget: dict[str, Any] = Field(default_factory=dict)
    reserved_budget: dict[str, Any] = Field(default_factory=dict)
    step_counts: dict[str, int] = Field(default_factory=dict)
    stop_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)


class RunCheckpoint(Strict):
    schema_version: Literal["rlar.checkpoint.v1"] = CHECKPOINT_SCHEMA_VERSION
    run_id: str
    cursor_line: int  # last input line fully processed
    committed_results: int
    run_consumed_budget: dict[str, Any] = Field(default_factory=dict)
    run_reserved_budget: dict[str, Any] = Field(default_factory=dict)
    deadline_utc: float | None = None
    episode: EpisodeCheckpoint | None = None
    journal_seq: int = 0


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / LATEST_NAME

    def write(self, checkpoint: RunCheckpoint) -> None:
        from .blobs import atomic_write_bytes

        atomic_write_bytes(self.path, checkpoint.model_dump_json().encode("utf-8"))

    def read(self) -> RunCheckpoint | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            # An atomically-replaced file is never half written; a corrupt file
            # here means external damage. Fall back to journal/results recovery
            # rather than trusting it.
            return None
        return RunCheckpoint.model_validate(data)
