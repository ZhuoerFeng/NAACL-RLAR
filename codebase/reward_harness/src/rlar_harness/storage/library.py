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
from typing import Literal

from ..schemas import (
    AGGREGATION_VERSION,
    LibraryEntry,
    RewardMode,
    TaskPack,
    ValidationReport,
)
from .blobs import BlobStore, append_line_durable
from .canonical import digest

LIBRARY_NAME = "reward_library.jsonl"

ReuseKind = Literal["reuse", "retest", "no_match"]


@dataclass(frozen=True)
class ReuseDecision:
    kind: ReuseKind
    entry: LibraryEntry | None
    reasons: list[str]
    #: Entries whose tags look related but which failed the trusted rule. Kept
    #: so the report can show that a "same label" match was correctly refused.
    rejected: list[tuple[str, list[str]]]


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
    def decide_reuse(
        self,
        snapshot: LibrarySnapshot,
        pack: TaskPack,
        mode: RewardMode,
        *,
        suite_version: str | None,
    ) -> ReuseDecision:
        rejected: list[tuple[str, list[str]]] = []
        retest_candidate: LibraryEntry | None = None
        retest_reasons: list[str] = []

        for entry in snapshot.entries:
            reasons: list[str] = []
            rule = entry.applicability
            pack_rule = pack.applicability_rule

            if rule.task_contract_digest != pack_rule.task_contract_digest:
                reasons.append("task_contract_digest mismatch")
            if not set(rule.permitted_inputs).issubset(set(pack.permitted_inputs)):
                reasons.append("entry requires inputs the current profile does not permit")
            if entry.definition.mode != mode:
                reasons.append("artifact mode differs from requested mode")
            if mode not in rule.mode_constraints:
                reasons.append(f"entry is not declared applicable to mode={mode}")
            if mode not in pack.mode_constraints:
                reasons.append(f"profile does not permit mode={mode}")
            for api in _required_apis(entry):
                if api not in pack.permitted_apis:
                    reasons.append(f"entry uses API {api!r} not permitted by this profile")

            evidence_reasons = self._evidence_reasons(entry, pack, suite_version)
            runtime_mismatch = rule.runtime_fingerprint != pack_rule.runtime_fingerprint

            if reasons:
                rejected.append((entry.reward_key, reasons))
                continue

            if not runtime_mismatch and not evidence_reasons:
                return ReuseDecision(
                    kind="reuse",
                    entry=entry,
                    reasons=[
                        "task contract, permitted inputs, mode, APIs and runtime "
                        "fingerprint all match; stored validation evidence is current"
                    ],
                    rejected=rejected,
                )

            # Structurally applicable but the evidence is stale: a deterministic
            # re-test settles it without asking the controller anything.
            if retest_candidate is None:
                retest_candidate = entry
                retest_reasons = (
                    (["runtime fingerprint changed"] if runtime_mismatch else [])
                    + evidence_reasons
                )
            else:
                rejected.append((entry.reward_key, evidence_reasons or ["runtime changed"]))

        if retest_candidate is not None:
            return ReuseDecision(
                kind="retest",
                entry=retest_candidate,
                reasons=retest_reasons,
                rejected=rejected,
            )
        return ReuseDecision(kind="no_match", entry=None, reasons=[], rejected=rejected)

    def _evidence_reasons(
        self, entry: LibraryEntry, pack: TaskPack, suite_version: str | None
    ) -> list[str]:
        reasons: list[str] = []
        summary = entry.validation_summary or {}
        if not summary.get("eligible"):
            reasons.append("stored validation report is not eligible")
        if summary.get("verifier_version") != pack.verifier_version:
            reasons.append("verifier_version changed since validation")
        if suite_version is not None and summary.get("suite_version") != suite_version:
            reasons.append("dev suite version changed since validation")
        if summary.get("aggregation_version") != AGGREGATION_VERSION:
            reasons.append("aggregation version changed since validation")
        if summary.get("profile_id") != pack.profile_id:
            reasons.append("validation was produced under a different profile")
        if not entry.validation_ref or not self.blobs.exists(entry.validation_ref):
            reasons.append("validation report blob is missing")
        return reasons

    def load_report(self, entry: LibraryEntry) -> ValidationReport:
        return ValidationReport.model_validate(self.blobs.get_json(entry.validation_ref))


def _required_apis(entry: LibraryEntry) -> list[str]:
    apis: list[str] = []
    for component in entry.definition.components:
        apis.extend(component.required_apis)
    return sorted(set(apis))
