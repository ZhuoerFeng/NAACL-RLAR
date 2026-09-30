"""Reward function library: a simple, append-only, progressive memory.

The library is *not* a semantic retrieval system. A hash locates content; it
never establishes applicability. Reuse without a controller call requires the
trusted profile rule to confirm that the task contract, permitted inputs, mode
and runtime all match, and that the stored validation evidence is still valid
under the current suite and verifier versions. An agent asserting "this fits"
or a matching text tag is not sufficient evidence.

Only entries that have been committed *and* published by a result row are
visible to later queries; an orphaned entry left behind by a crash between the
library append and the result append is never retrievable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..schemas import (
    LibraryEntry,
    RewardMode,
    TaskPack,
)
from .blobs import BlobStore, append_line_durable
from .canonical import digest

LIBRARY_NAME = "reward_library.jsonl"


@dataclass(frozen=True)
class LibrarySnapshot:
    """A frozen view of the library taken at the start of an episode."""

    snapshot_id: str
    entries: tuple[LibraryEntry, ...]

    def __len__(self) -> int:
        return len(self.entries)


class RewardLibrary:
    def __init__(self, path: Path, blobs: BlobStore) -> None:
        self.path = Path(path)
        self.blobs = blobs
        self._entries: list[LibraryEntry] = []
        self._by_key: dict[str, LibraryEntry] = {}
        #: Entries appended to the file but not yet published by a result row.
        self._unpublished: list[LibraryEntry] = []

    # -- load / rebuild -------------------------------------------------
    def rebuild_index(self, published_result_seqs: set[int]) -> None:
        """Rebuild the in-memory index from the library file.

        ``published_result_seqs`` is the set of result-commit sequence numbers
        recovered from ``results.jsonl``. An entry whose publishing result never
        landed is loaded as *unpublished* and stays invisible to reuse.
        """
        self._entries = []
        self._by_key = {}
        self._unpublished = []
        if not self.path.exists():
            return
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raw = raw[: raw.rfind(b"\n") + 1]  # torn tail: never committed
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            entry = LibraryEntry.model_validate(json.loads(line))
            if entry.published_by_result_seq in published_result_seqs:
                self._entries.append(entry)
                self._by_key[entry.reward_key] = entry
            else:
                self._unpublished.append(entry)

    # -- accessors ------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    @property
    def unpublished(self) -> list[LibraryEntry]:
        return list(self._unpublished)

    def get(self, reward_key: str) -> LibraryEntry | None:
        return self._by_key.get(reward_key)

    def snapshot(self) -> LibrarySnapshot:
        entries = tuple(self._entries)
        snapshot_id = digest(
            {
                "count": len(entries),
                "keys": [e.reward_key for e in entries],
            }
        )
        return LibrarySnapshot(snapshot_id=snapshot_id, entries=entries)

    # -- append ---------------------------------------------------------
    def append(self, entry: LibraryEntry) -> None:
        """Append durably to the file. The entry is not visible until published."""
        append_line_durable(self.path, entry.model_dump_json())
        self._unpublished.append(entry)

    def publish(self, reward_key: str) -> None:
        """Mark a previously appended entry visible, after its result committed."""
        for i, entry in enumerate(self._unpublished):
            if entry.reward_key == reward_key:
                self._unpublished.pop(i)
                self._entries.append(entry)
                self._by_key[entry.reward_key] = entry
                return

    # -- reuse ----------------------------------------------------------


    def compatible_entries(self, snapshot: LibrarySnapshot, pack: TaskPack, mode: RewardMode):
        """Filter task/runtime applicability. Only the finalizer can authorize reuse."""
        rule = pack.applicability_rule
        return tuple(entry for entry in snapshot.entries
            if entry.definition.mode == mode and mode in pack.mode_constraints
            and mode in entry.applicability.mode_constraints
            and entry.applicability.task_contract_digest == rule.task_contract_digest
            and entry.applicability.runtime_fingerprint == rule.runtime_fingerprint
            and set(entry.applicability.permitted_inputs) <= set(pack.permitted_inputs)
            and entry.definition.runtime_contract.reward_logic_policy == pack.reward_logic_policy
            and all(set(c.required_apis) <= set(pack.permitted_apis) for c in entry.definition.components))
