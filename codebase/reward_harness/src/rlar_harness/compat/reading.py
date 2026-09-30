"""Read-only views of stored evidence, without applying today's schema defaults.

These objects cannot be submitted to an execution API. Keeping serialized data
unchanged preserves historical config digests, reward keys and report signatures.
"""
from copy import deepcopy
import json
from pathlib import Path


class StoredRecord:
    def __init__(self, data):
        self._data = deepcopy(data)

    def __getattr__(self, name):
        try:
            value = self._data[name]
        except KeyError as exc:
            raise AttributeError(name) from exc
        return StoredRecord(value) if isinstance(value, dict) else deepcopy(value)

    def model_dump(self, **kwargs):
        data = deepcopy(self._data)
        for key in kwargs.get('exclude', ()):
            data.pop(key, None)
        return data


def read_results(path):
    """Only newline-committed records count; never repair or rewrite history."""
    path = Path(path)
    if not path.exists():
        return []
    raw = path.read_bytes()
    if raw and not raw.endswith(b'\n'):
        raw = raw[:raw.rfind(b'\n') + 1]
    return [StoredRecord(json.loads(line)) for line in raw.splitlines() if line.strip()]
