"""Read-only streaming input adapters.

The source file is opened read-only and never rewritten. Malformed lines,
missing fields and duplicate ids all produce an explicit error item carrying
the input location, and the stream continues with the next record.

Duplicate policy (fixed engineering default, recorded in the manifest): within
one input stream, the *first* occurrence of a ``query_id`` is processed
normally; every later occurrence — even a byte-identical one — is reported as
``duplicate_query_id`` and gets a position-derived error key, so it can never
overwrite the first result. Re-reading the same physical line during a resume
is not a duplicate.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .errors import Code, ErrorCategory, RetryOwner
from .schemas import InputLocation, QueryRecord, StructuredError
from .storage.canonical import digest


@dataclass(frozen=True)
class InputItem:
    location: InputLocation
    record: QueryRecord | None
    error: StructuredError | None
    raw_digest: str

    @property
    def ok(self) -> bool:
        return self.record is not None

    def error_key(self) -> str:
        """Position-derived key for a rejected line."""
        return "input_error:" + digest(
            {"path": self.location.path, "line": self.location.line}
        )


def file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _error(location: InputLocation, code: str, message: str) -> StructuredError:
    return StructuredError(
        category=ErrorCategory.INPUT,
        code=code,
        phase="input_validation",
        retry_owner=RetryOwner.NONE,
        message=message,
        suggested_recovery="fix the input record; the rest of the stream is unaffected",
    )


def iter_jsonl(path: Path, *, start_line: int = 1) -> Iterator[InputItem]:
    """Stream a JSONL input file. ``start_line`` is 1-based and inclusive."""
    path = Path(path)
    seen_ids: dict[str, int] = {}
    with open(path, encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            location = InputLocation(path=str(path), line=lineno)
            raw_digest = digest({"raw": raw})

            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                if lineno >= start_line:
                    yield InputItem(
                        location,
                        None,
                        _error(location, Code.MALFORMED_RECORD, f"invalid JSON: {exc}"),
                        raw_digest,
                    )
                continue

            if not isinstance(data, dict):
                if lineno >= start_line:
                    yield InputItem(
                        location,
                        None,
                        _error(
                            location,
                            Code.MALFORMED_RECORD,
                            f"record must be a JSON object, got {type(data).__name__}",
                        ),
                        raw_digest,
                    )
                continue

            try:
                record = QueryRecord.model_validate(data)
            except Exception as exc:
                if lineno >= start_line:
                    yield InputItem(
                        location,
                        None,
                        _error(location, Code.MALFORMED_RECORD, str(exc)),
                        raw_digest,
                    )
                continue

            first_seen = seen_ids.get(record.query_id)
            if first_seen is not None:
                # Duplicate ids are tracked across the whole file, including the
                # part skipped by ``start_line``, so a resume cannot turn a
                # duplicate into a fresh record.
                if lineno >= start_line:
                    yield InputItem(
                        location,
                        None,
                        _error(
                            location,
                            Code.DUPLICATE_QUERY_ID,
                            f"query_id {record.query_id!r} already appeared at line "
                            f"{first_seen}; later occurrences are never processed",
                        ),
                        raw_digest,
                    )
                continue
            seen_ids[record.query_id] = lineno

            if lineno >= start_line:
                yield InputItem(location, record, None, raw_digest)


def record_digest(record: QueryRecord) -> str:
    return digest(record.model_dump(mode="json"))


def job_key(record_digest_: str, config_digest: str, library_snapshot: str) -> str:
    """Logical identity of one construction job.

    Includes the input content, the configuration fingerprint and the library
    snapshot taken when the job started, so a resumed job recomputes the same
    key rather than picking up a library that has grown since.
    """
    return digest(
        {
            "input_digest": record_digest_,
            "config_digest": config_digest,
            "library_snapshot": library_snapshot,
        }
    )
