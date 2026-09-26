"""Task pack and development suite loading.

A task pack is the versioned contract for a task profile: what the reward is
supposed to measure, which inputs and APIs it may touch, which modes are
allowed, and the pre-declared acceptance policy.

Audit suites are loaded through a *separate* entry point with a separate root
and are never reachable from the controller's resource index.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from ..schemas import DevSuite, TaskPack


class TaskPackStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[str, TaskPack] = {}

    def get(self, profile_id: str) -> TaskPack:
        if profile_id in self._cache:
            return self._cache[profile_id]
        if not profile_id or Path(profile_id).name != profile_id or profile_id in (".", ".."):
            raise ConfigError("profile ID must be a resource ID, not a path")
        path = self.root / f"{profile_id}.json"
        if not path.exists():
            raise ConfigError(f"unknown task_profile_id {profile_id!r} (looked in {path})")
        try:
            pack = TaskPack.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise ConfigError(f"invalid task pack {path}: {exc}") from exc
        if pack.profile_id != profile_id:
            raise ConfigError(
                f"task pack {path} declares profile_id={pack.profile_id!r} "
                f"but is filed under {profile_id!r}"
            )
        self._cache[profile_id] = pack
        return pack

    def available(self) -> list[str]:
        return sorted(p.stem for p in self.root.glob("*.json"))


class DevSuiteStore:
    """Holds oracle labels. Nothing here is ever passed into a worker."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._cache: dict[str, DevSuite] = {}

    def get(self, suite_ref: str) -> DevSuite:
        if suite_ref in self._cache:
            return self._cache[suite_ref]
        if not suite_ref or Path(suite_ref).name != suite_ref or suite_ref in (".", ".."):
            raise ConfigError("suite reference must be a resource ID, not a path")
        path = self.root / f"{suite_ref}.json"
        if not path.exists():
            raise ConfigError(f"dev suite {suite_ref!r} not found at {path}")
        suite = DevSuite.model_validate(json.loads(path.read_text(encoding="utf-8")))
        self._cache[suite_ref] = suite
        return suite

    def try_get(self, suite_ref: str | None) -> DevSuite | None:
        if not suite_ref:
            return None
        try:
            return self.get(suite_ref)
        except ConfigError:
            return None


def whitelist_example(
    case_example: dict[str, Any], permitted_inputs: list[str]
) -> dict[str, Any]:
    """Project a dev case's example down to the profile's permitted inputs.

    This is the boundary that keeps oracle labels, dataset handles and audit
    tags out of the execution worker. Anything not explicitly permitted is
    dropped rather than passed through.
    """
    return {k: v for k, v in case_example.items() if k in permitted_inputs}
