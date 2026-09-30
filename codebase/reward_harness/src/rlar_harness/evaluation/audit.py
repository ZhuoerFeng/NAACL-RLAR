"""Prevent audit inputs from reaching synthesis resources."""
import json
from pathlib import Path
from typing import Any
from ..errors import PreflightError, Code

def assert_not_controller_visible(resource_index: dict[str, Any], audit_root: Path | None) -> None:
    """Fail loudly if an audit path ever leaks into the controller's resources.

    Cheap to check and catastrophic to get wrong, so it is asserted at run
    start rather than left to review.
    """
    if audit_root is None:
        return
    needle = str(audit_root.resolve())
    for key, value in resource_index.items():
        rendered = json.dumps(value, default=str)
        if needle in rendered or "audit" in str(key).lower():
            raise PreflightError(
                f"resource {key!r} exposes the audit suite root to the "
                "controller; audit material must stay outside the episode",
                [Code.RUNNER_CAPABILITY_MISSING],
            )
