"""``results.jsonl`` — the logical commit log.

A query is done when, and only when, its result row is durably appended here.
A definition in the library, a blob on disk, or a checkpoint entry is not a
commit. Recovery scans this file first and skips every ``job_key`` it finds.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..schemas import ConstructionResult
from .blobs import append_line_durable

RESULTS_NAME = "results.jsonl"


class ResultsStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._results: list[ConstructionResult] = []
        self._by_job_key: dict[str, ConstructionResult] = {}

    def load(self, *, repair_tail: bool = False) -> list[ConstructionResult]:
        """Load committed results. An incomplete trailing line is not a commit."""
        self._results = []
        self._by_job_key = {}
        if not self.path.exists():
            return []
        raw = self.path.read_bytes()
        if raw and not raw.endswith(b"\n"):
            raw = raw[: raw.rfind(b"\n") + 1]
            if repair_tail:
                from .blobs import atomic_write_bytes
                atomic_write_bytes(self.path, raw)
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            result = ConstructionResult.model_validate(json.loads(line))
            self._results.append(result)
            self._by_job_key[result.job_key] = result
        return list(self._results)

    @property
    def results(self) -> list[ConstructionResult]:
        return list(self._results)

    @property
    def next_seq(self) -> int:
        """1-based sequence number the next committed row will occupy."""
        return len(self._results) + 1

    def committed_seqs(self) -> set[int]:
        return set(range(1, len(self._results) + 1))

    def has_job(self, job_key: str) -> bool:
        return job_key in self._by_job_key

    def get_job(self, job_key: str) -> ConstructionResult | None:
        return self._by_job_key.get(job_key)

    def append(self, result: ConstructionResult) -> int:
        """Durably commit a result row. Returns its 1-based sequence number."""
        if result.job_key in self._by_job_key:
            return self._results.index(self._by_job_key[result.job_key]) + 1
        append_line_durable(self.path, result.model_dump_json())
        self._results.append(result)
        self._by_job_key[result.job_key] = result
        return len(self._results)
