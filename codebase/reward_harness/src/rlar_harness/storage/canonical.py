"""Canonical JSON serialization and content hashing.

``reward_key = SHA256(canonical_json(validated_definition))``.

The canonical form is pinned by golden fixtures in ``tests/golden/``. Changing
any rule here changes every reward key in existence, so the rules are spelled
out explicitly:

1. The object is first validated and dumped through its Pydantic model, which
   fills schema defaults and fixes numeric types. Hashing a raw dict that is
   missing defaults would give a different key for a semantically identical
   definition.
2. Python source strings have CRLF and lone CR normalized to LF. Source is
   otherwise untouched: not stripped, not reformatted, not re-indented.
3. JSON is emitted as UTF-8 with sorted object keys, compact separators, and
   ``ensure_ascii=False``. List order is preserved and significant.
4. Non-finite numbers (NaN, Infinity) are rejected outright.
5. Floats that are integral are *not* collapsed to ints, and ints are not
   widened to floats; the Pydantic dump decides the type and we keep it.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from pydantic import BaseModel

CANONICAL_VERSION = "canonical.v1"

#: Keys whose string values are treated as Python source and line-normalized.
_SOURCE_KEYS = frozenset({"source"})


def normalize_source(text: str) -> str:
    """Normalize line endings only. Never strip or reformat code."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _canonicalize(value: Any, *, in_source_key: bool = False, path: str = "$") -> Any:
    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="json"), path=path)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"{path}: non-finite float cannot be canonicalized")
        return value
    if value is None:
        return None
    if isinstance(value, str):
        return normalize_source(value) if in_source_key else value
    if isinstance(value, (list, tuple)):
        return [_canonicalize(v, path=f"{path}[{i}]") for i, v in enumerate(value)]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in value:
            if not isinstance(key, str):
                raise ValueError(f"{path}: object keys must be strings, got {type(key)!r}")
            out[key] = _canonicalize(
                value[key],
                in_source_key=key in _SOURCE_KEYS,
                path=f"{path}.{key}",
            )
        return out
    raise ValueError(f"{path}: {type(value).__name__} is not canonicalizable")


def canonical_json(value: Any) -> bytes:
    """Return the canonical UTF-8 JSON encoding of ``value``."""
    prepared = _canonicalize(value)
    text = json.dumps(
        prepared,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return text.encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(value: Any) -> str:
    """Canonical SHA-256 (lowercase hex) of any JSON-compatible value."""
    return sha256_hex(canonical_json(value))


def reward_key_for(definition: Any) -> str:
    """Compute the reward key of a *validated* :class:`RewardDefinition`.

    The key covers source, criteria, normalization, component order, aggregation
    and the runtime contract. It deliberately does not cover the query id, the
    current reference answer, or any validation report: adding a report to an
    artifact must not change its identity.
    """
    from ..schemas import RewardDefinition

    if not isinstance(definition, RewardDefinition):
        definition = RewardDefinition.model_validate(definition)
    return digest(definition)


def text_digest(text: str) -> str:
    return sha256_hex(text.encode("utf-8"))
