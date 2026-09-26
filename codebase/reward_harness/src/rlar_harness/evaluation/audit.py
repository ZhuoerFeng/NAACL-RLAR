"""Audit: the held-out check that the construction loop can never see.

Development validation decides whether a reward is eligible to be committed.
Audit decides whether the *harness* is any good. If the same suite drove both,
the number an audit produced would be a training-set number wearing a test-set
label, so the separation here is structural rather than procedural:

* audit suites live under their own root (``validation.audit_suite_root``),
  which is never added to the controller's resource index and never rendered
  into a prompt;
* the audit entry point is offline — it takes already-committed rewards and
  re-scores them; it cannot start an episode, call the controller, or write to
  the reward library;
* audit reports are written to their own namespace and are not readable by any
  construction tool.

Audit also refuses to run on a backend that cannot isolate the code it
executes. A number produced by running model-written code in a process that can
read the audit labels off the filesystem is not an audit result, and reporting
it as one would be the exact failure this module exists to prevent. The refusal
is a hard preflight error, not a warning.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..errors import Code, PreflightError
from ..schemas import (
    Assurance,
    DevSuite,
    RewardDefinition,
    TaskPack,
    ValidationReport,
)
from ..storage.canonical import canonical_json
from .evaluator import TrustedEvaluator, ValidationOutcome

#: Ordered weakest-to-strongest. An audit that requires ``isolated`` will not
#: accept ``behavioral_prototype``.
ASSURANCE_ORDER: dict[Assurance, int] = {
    "static": 0,
    "behavioral_prototype": 1,
    "isolated": 2,
}


def assurance_meets(actual: Assurance, required: Assurance) -> bool:
    return ASSURANCE_ORDER[actual] >= ASSURANCE_ORDER[required]


@dataclass
class AuditResult:
    reward_key: str
    profile_id: str
    suite_id: str
    report: ValidationReport
    outcome: ValidationOutcome


@dataclass
class AuditSummary:
    audit_id: str
    created_at: str
    assurance: Assurance
    required_assurance: Assurance
    suite_root: str
    results: list[AuditResult] = field(default_factory=list)
    #: Recorded verbatim in the report so no downstream reader can mistake a
    #: prototype-grade number for an isolated one.
    caveats: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "created_at": self.created_at,
            "assurance": self.assurance,
            "required_assurance": self.required_assurance,
            "suite_root": self.suite_root,
            "caveats": list(self.caveats),
            "rewards": [
                {
                    "reward_key": r.reward_key,
                    "profile_id": r.profile_id,
                    "suite_id": r.suite_id,
                    "eligible_under_audit": r.report.eligible,
                    "ineligibility_reasons": list(r.report.ineligibility_reasons),
                    "metrics": r.report.metrics.model_dump(mode="json"),
                    "infrastructure_failures": list(r.outcome.infrastructure_failures),
                }
                for r in self.results
            ],
        }


class AuditSuiteStore:
    """Loads audit suites. Separate root, separate cache, no controller access."""

    def __init__(self, root: Path | str | None) -> None:
        self.root = Path(root) if root else None
        self._cache: dict[str, DevSuite] = {}

    @property
    def configured(self) -> bool:
        return self.root is not None and self.root.exists()

    def get(self, suite_ref: str) -> DevSuite:
        if self.root is None:
            raise PreflightError(
                "no audit_suite_root is configured; there is no held-out suite "
                "to audit against"
            )
        if suite_ref in self._cache:
            return self._cache[suite_ref]
        path = self.root / f"{suite_ref}.json"
        if not path.exists():
            raise PreflightError(f"audit suite {suite_ref!r} not found at {path}")
        suite = DevSuite.model_validate(json.loads(path.read_text(encoding="utf-8")))
        self._cache[suite_ref] = suite
        return suite

    def available(self) -> list[str]:
        if self.root is None or not self.root.exists():
            return []
        return sorted(p.stem for p in self.root.glob("*.json"))


class Auditor:
    """Offline re-scoring of committed rewards against a held-out suite."""

    def __init__(
        self,
        evaluator: TrustedEvaluator,
        suites: AuditSuiteStore,
        *,
        required_assurance: Assurance = "isolated",
    ) -> None:
        if evaluator.namespace != "audit":
            raise ValueError(
                "the auditor requires an evaluator constructed with "
                "namespace='audit' so its reports cannot be confused with "
                "development validation reports"
            )
        self.evaluator = evaluator
        self.suites = suites
        self.required_assurance = required_assurance

    # -- preflight --------------------------------------------------------
    def preflight(self) -> list[str]:
        """Return every blocking problem at once. Empty means the audit may run."""
        problems: list[str] = []
        if not self.suites.configured:
            problems.append(
                "validation.audit_suite_root is not configured or does not exist"
            )
        actual = self.evaluator.assurance()
        if not assurance_meets(actual, self.required_assurance):
            caps = self.evaluator.runner.capabilities()
            missing = [
                name
                for name, ok in (
                    ("filesystem_isolation", caps.filesystem_isolation),
                    ("network_isolation", caps.network_isolation),
                    ("label_secret_isolation", caps.label_secret_isolation),
                )
                if not ok
            ]
            problems.append(
                f"runner assurance is {actual!r} but this audit requires "
                f"{self.required_assurance!r}; missing capabilities: {missing}. "
                "Running model-written code without these does not produce an "
                "audit result, so the audit is refused rather than annotated."
            )
        return problems

    def require_preflight(self) -> None:
        problems = self.preflight()
        if problems:
            raise PreflightError(
                "audit preflight failed:\n  - " + "\n  - ".join(problems),
                problems,
            )

    # -- running ----------------------------------------------------------
    def audit(
        self,
        entries: list[tuple[str, RewardDefinition, TaskPack, str]],
        *,
        allow_insufficient_assurance: bool = False,
    ) -> AuditSummary:
        """Re-score committed rewards.

        ``entries`` is ``(reward_key, definition, task_pack, audit_suite_ref)``.

        ``allow_insufficient_assurance`` exists for local experimentation only.
        It downgrades the refusal into a recorded caveat and marks every number
        in the summary as prototype-grade; it does not make the result an audit.
        """
        problems = self.preflight()
        caveats: list[str] = []
        if problems:
            if not allow_insufficient_assurance:
                raise PreflightError(
                    "audit preflight failed:\n  - " + "\n  - ".join(problems),
                    problems,
                )
            caveats.extend(
                f"NOT A VALID AUDIT RESULT: {p}" for p in problems
            )

        results: list[AuditResult] = []
        for reward_key, definition, pack, suite_ref in entries:
            suite = self.suites.get(suite_ref)
            outcome = self.evaluator.validate(
                definition,
                pack,
                suite,
                reward_key=reward_key,
                action_id=f"audit:{reward_key[:12]}",
                policy=pack.acceptance_policy,
            )
            results.append(
                AuditResult(
                    reward_key=reward_key,
                    profile_id=pack.profile_id,
                    suite_id=suite.suite_id,
                    report=outcome.report,
                    outcome=outcome,
                )
            )

        return AuditSummary(
            audit_id=uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc).isoformat(),
            assurance=self.evaluator.assurance(),
            required_assurance=self.required_assurance,
            suite_root=str(self.suites.root) if self.suites.root else "",
            results=results,
            caveats=caveats,
        )

    def write_summary(self, summary: AuditSummary, path: Path) -> Path:
        """Write to the audit namespace. Never into the run's resource index."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical_json(summary.to_json()))
        return path


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
