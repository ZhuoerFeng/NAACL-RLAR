"""Content-addressed blob store with durable, atomic writes.

Full observations, assistant messages, request bodies and validation reports
live here; the journal and results only carry ``blob:<sha256>`` references.
Writes go temp file -> flush -> fsync -> atomic rename, and the containing
directory is fsynced as well, so a reference that has been journaled always
resolves after a crash.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from ..errors import BlobMissingError, StorageError
from .canonical import canonical_json, sha256_hex

BLOB_PREFIX = "blob:"


def is_blob_ref(value: str) -> bool:
    return isinstance(value, str) and value.startswith(BLOB_PREFIX)


class BlobStore:
    def __init__(self, root: Path, max_bytes: int | None = None) -> None:
        self.max_bytes = max_bytes
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- paths ---------------------------------------------------------
    def _path_for(self, hexdigest: str) -> Path:
        return self.root / hexdigest[:2] / f"{hexdigest}.blob"

    def ref_to_path(self, ref: str) -> Path:
        if not is_blob_ref(ref):
            raise StorageError(f"not a blob ref: {ref!r}")
        return self._path_for(ref[len(BLOB_PREFIX) :])

    # -- writes --------------------------------------------------------
    def put_bytes(self, data: bytes) -> str:
        if self.max_bytes is not None and len(data) > self.max_bytes:
            raise StorageError("blob exceeds configured storage limit")
        hexdigest = sha256_hex(data)
        target = self._path_for(hexdigest)
        if target.exists():
            self.get_bytes(BLOB_PREFIX + hexdigest)
            return BLOB_PREFIX + hexdigest
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, target)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        _fsync_dir(target.parent)
        return BLOB_PREFIX + hexdigest

    def put_text(self, text: str) -> str:
        return self.put_bytes(text.encode("utf-8"))

    def put_json(self, value: Any) -> str:
        return self.put_bytes(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                        separators=(",", ":"), allow_nan=False).encode("utf-8"))

    # -- reads ---------------------------------------------------------
    def get_bytes(self, ref: str) -> bytes:
        path = self.ref_to_path(ref)
        if not path.exists():
            raise BlobMissingError(f"blob not found: {ref}")
        data = path.read_bytes()
        expected = ref[len(BLOB_PREFIX) :]
        actual = sha256_hex(data)
        if actual != expected:
            raise StorageError(
                f"blob content hash mismatch for {ref}: stored content hashes to {actual}"
            )
        return data

    def get_text(self, ref: str) -> str:
        return self.get_bytes(ref).decode("utf-8")

    def get_json(self, ref: str) -> Any:
        return json.loads(self.get_text(ref))

    def exists(self, ref: str) -> bool:
        try:
            return self.ref_to_path(ref).exists()
        except StorageError:
            return False


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        # Some filesystems refuse directory fsync; the rename is still atomic.
        pass
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Durably replace ``path`` with ``data``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def append_line_durable(path: Path, line: str) -> int:
    """Append one JSONL line and fsync it. Returns the byte offset written at."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = line.rstrip("\n") + "\n"
    with open(path, "a", encoding="utf-8") as fh:
        offset = fh.tell()
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    return offset
