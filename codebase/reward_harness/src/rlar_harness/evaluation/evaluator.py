"""The trusted evaluator: the only thing allowed to say a reward is eligible.

It holds the oracle labels, runs the candidate through the runner with a
whitelisted view of each example, computes the metrics itself, and applies the
pre-declared acceptance policy. An agent-written "PASS" file is not evidence,
and neither is a report whose body has been edited: every report carries an
HMAC over its canonical body keyed by a per-run secret the worker never sees.

Metric conventions that matter, and are easy to get wrong:

* Denominators keep **every planned case**. A case that failed to score is not
  quietly removed, and it counts as neither a correct rejection nor a correct
  acceptance.
* Ranking pairs that could not be scored count as **failures**, so a reward
  that crashes on hard examples cannot post a high accuracy on the easy subset.
* Infrastructure failures are reported separately from quality failures. A
  worker that died is not evidence that the reward is semantically wrong, and
  the episode must treat it as an environment retry rather than telling the
  agent to rewrite its code.
"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..errors import RetryOwner
from ..runtime.aggregate import aggregate
from ..runtime.runner import Runner, make_execute_request
from ..schemas import (
    AGGREGATION_VERSION,
    AcceptancePolicy,
    Assurance,
    DevCase,
    DevSuite,
    RewardDefinition,
    ScoreResult,
    TaskPack,
    Usage,
    ValidationMetrics,
    ValidationReport,
)
from ..storage.canonical import canonical_json, digest
from .taskpack import whitelist_example

#: Error codes that mean "the environment failed", not "the reward is wrong".
INFRASTRUCTURE_CODES = {
    "worker_unavailable",
    "scoring_service_error",
    "auth_failed",
    "scoring_budget_exhausted",
}


@dataclass
class ExecutionLimits:
    wall_timeout_s: float = 5.0
    cpu_timeout_s: float = 5.0
    memory_limit_mb: int | None = None
    max_output_bytes: int = 65536
    max_return_bytes: int = 262144


@dataclass
class ValidationOutcome:
    report: ValidationReport
    #: Non-empty when the run could not be completed for environmental reasons.
    infrastructure_failures: list[str] = field(default_factory=list)
    score_results: list[ScoreResult] = field(default_factory=list)


class TrustedEvaluator:
    def __init__(
        self,
        runner: Runner,
        *,
        limits: ExecutionLimits | None = None,
        integrity_secret: bytes | None = None,
        normalization_mappings: dict[str, dict[str, Any]] | None = None,
        namespace: str = "dev",
    ) -> None:
        self.runner = runner
        self.limits = limits or ExecutionLimits()
        self._secret = integrity_secret or uuid.uuid4().bytes
        self.normalization_mappings = normalization_mappings or {}
        #: ``dev`` or ``audit``. Audit reports live in their own namespace and
        #: are never surfaced to the controller.
        self.namespace = namespace

    # -- assurance -------------------------------------------------------
    def assurance(self) -> Assurance:
        caps = self.runner.capabilities()
        if (
            caps.filesystem_isolation
            and caps.network_isolation
            and caps.label_secret_isolation
        ):
            return "isolated"
        return "behavioral_prototype"

    # -- scoring ---------------------------------------------------------
    def score_examples(
        self,
        definition: RewardDefinition,
        pack: TaskPack,
        examples: list[dict[str, Any]],
        example_ids: list[str],
        *,
        action_id: str,
        reward_key: str,
    ) -> tuple[list[ScoreResult], Usage]:
        if not examples:
            return [], Usage()
        request = make_execute_request(
            definition=definition,
            examples=examples,
            example_ids=example_ids,
            permitted_apis=pack.permitted_apis,
            runtime_fingerprint=pack.applicability_rule.runtime_fingerprint,
            wall_timeout_s=self.limits.wall_timeout_s,
            cpu_timeout_s=self.limits.cpu_timeout_s,
            memory_limit_mb=self.limits.memory_limit_mb,
            max_output_bytes=self.limits.max_output_bytes,
            max_return_bytes=self.limits.max_return_bytes,
            action_id=action_id,
        )
        response = self.runner.execute(request)
        mappings = {**self.normalization_mappings, **pack.normalization_mappings}
        results = [
            aggregate(
                definition,
                response.per_example[i],
                reward_key=reward_key,
                mappings=mappings,
                example_id=example_ids[i],
            )
            for i in range(len(example_ids))
        ]
        usage = response.usage.merged(Usage(test_cases=len(example_ids)))
        return results, usage

    # -- validation ------------------------------------------------------
    def validate(
        self,
        definition: RewardDefinition,
        pack: TaskPack,
        suite: DevSuite,
        *,
        reward_key: str,
        action_id: str,
        policy: AcceptancePolicy | None = None,
    ) -> ValidationOutcome:
        policy = policy or pack.acceptance_policy
        started = time.monotonic()
        cases = list(suite.cases)
        example_ids = [c.case_id for c in cases]
        examples = [whitelist_example(c.example, pack.permitted_inputs) for c in cases]

        results, usage = self.score_examples(
            definition,
            pack,
            examples,
            example_ids,
            action_id=action_id,
            reward_key=reward_key,
        )
        usage = usage.merged(Usage(wall_seconds=time.monotonic() - started))

        by_case = {r.example_id: r for r in results}
        infra = self._infrastructure_failures(results)
        metrics, error_categories = self._compute_metrics(cases, by_case, policy)
        eligible, reasons = self._decide(metrics, policy, infra)

        report = ValidationReport(
            report_id=uuid.uuid4().hex,
            reward_key=reward_key,
            task_contract_digest=pack.applicability_rule.task_contract_digest,
            suite_id=suite.suite_id,
            suite_version=suite.version,
            suite_digest=digest(suite),
            verifier_version=pack.verifier_version,
            runtime_fingerprint=pack.applicability_rule.runtime_fingerprint,
            aggregation_version=AGGREGATION_VERSION,
            profile_id=pack.profile_id,
            case_ids=example_ids,
            metrics=metrics,
            per_case=results,
            error_categories=error_categories,
            eligible=eligible,
            ineligibility_reasons=reasons,
            assurance=self.assurance(),
            policy=policy,
            usage=usage,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        report = self.sign(report)
        return ValidationOutcome(
            report=report, infrastructure_failures=infra, score_results=results
        )

    def static_only_report(
        self,
        definition: RewardDefinition,
        pack: TaskPack,
        *,
        reward_key: str,
        reason: str,
    ) -> ValidationReport:
        """Report for a definition that has no trustworthy test basis.

        This is what backs the ``unvalidated`` construction status. It is never
        eligible, and it says so in the assurance field rather than in prose.
        """
        metrics = ValidationMetrics(
            planned_cases=0,
            completed_cases=0,
            failed_cases=0,
            execution_error_rate=None,
            false_accept_rate=None,
            false_reject_rate=None,
            ranking_pairs_planned=0,
            ranking_pairs_scored=0,
            ranking_pairs_missing=0,
            ranking_accuracy=None,
            invariance_pairs_planned=0,
            invariance_violations=0,
            invariance_violation_rate=None,
            coverage=0.0,
            limitations=[
                "no development suite was available; only schema and syntax were checked",
                "static checks cannot establish that this reward generalizes",
            ],
        )
        report = ValidationReport(
            report_id=uuid.uuid4().hex,
            reward_key=reward_key,
            task_contract_digest=pack.applicability_rule.task_contract_digest,
            suite_id="",
            suite_version="",
            verifier_version=pack.verifier_version,
            runtime_fingerprint=pack.applicability_rule.runtime_fingerprint,
            profile_id=pack.profile_id,
            case_ids=[],
            metrics=metrics,
            per_case=[],
            eligible=False,
            ineligibility_reasons=[reason],
            assurance="static",
            policy=pack.acceptance_policy,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return self.sign(report)

    # -- metrics ---------------------------------------------------------
    def _compute_metrics(
        self,
        cases: list[DevCase],
        by_case: dict[str | None, ScoreResult],
        policy: AcceptancePolicy,
    ) -> tuple[ValidationMetrics, dict[str, int]]:
        planned = len(cases)
        completed = 0
        failed = 0
        partial = 0
        all_failed = 0
        component_executions = 0
        component_errors = 0
        error_categories: dict[str, int] = {}
        mask_distribution: dict[str, int] = {}

        for case in cases:
            result = by_case.get(case.case_id)
            if result is None:
                failed += 1
                continue
            component_executions += result.planned_count
            for component in result.component_results:
                if component.status == "error" and component.error is not None:
                    component_errors += 1
                    error_categories[component.error.code] = (
                        error_categories.get(component.error.code, 0) + 1
                    )
            mask = ",".join(result.valid_component_ids) or "<none>"
            mask_distribution[mask] = mask_distribution.get(mask, 0) + 1
            if result.status == "failed":
                failed += 1
                all_failed += 1
            else:
                completed += 1
                if result.status == "partial":
                    partial += 1

        threshold = policy.score_threshold

        correct_cases = [c for c in cases if c.is_correct]
        incorrect_cases = [c for c in cases if not c.is_correct]

        def total_of(case: DevCase) -> float | None:
            result = by_case.get(case.case_id)
            return result.total_score if result is not None else None

        # Denominators keep all planned cases of each kind; an unscored case is
        # neither an accept nor a reject.
        false_accepts = sum(
            1 for c in incorrect_cases if (t := total_of(c)) is not None and t >= threshold
        )
        false_rejects = sum(
            1 for c in correct_cases if (t := total_of(c)) is not None and t < threshold
        )
        far = (false_accepts / len(incorrect_cases)) if incorrect_cases else None
        frr = (false_rejects / len(correct_cases)) if correct_cases else None

        # Ranking: every (correct, incorrect) pair inside a group is planned.
        pairs_planned = 0
        pairs_scored = 0
        pairs_correct = 0
        groups: dict[str, list[DevCase]] = {}
        for case in cases:
            groups.setdefault(case.group_id or case.case_id, []).append(case)
        for members in groups.values():
            positives = [c for c in members if c.is_correct]
            negatives = [c for c in members if not c.is_correct]
            for pos in positives:
                for neg in negatives:
                    pairs_planned += 1
                    tp, tn = total_of(pos), total_of(neg)
                    if tp is None or tn is None:
                        continue  # missing pair: counted as a failure below
                    pairs_scored += 1
                    if tp > tn + policy.tie_tolerance:
                        pairs_correct += 1
        ranking_accuracy = (pairs_correct / pairs_planned) if pairs_planned else None

        # Invariance: a declared-equivalent rewrite must not move the score.
        inv_planned = 0
        inv_violations = 0
        for case in cases:
            if case.kind != "invariance" or not case.base_case_id:
                continue
            inv_planned += 1
            t_case = total_of(case)
            base = next((c for c in cases if c.case_id == case.base_case_id), None)
            t_base = total_of(base) if base is not None else None
            if t_case is None or t_base is None:
                inv_violations += 1  # missing is a violation, not a free pass
            elif abs(t_case - t_base) > policy.tie_tolerance:
                inv_violations += 1

        metrics = ValidationMetrics(
            planned_cases=planned,
            completed_cases=completed,
            failed_cases=failed,
            execution_error_rate=(
                component_errors / component_executions if component_executions else None
            ),
            false_accept_rate=far,
            false_reject_rate=frr,
            ranking_pairs_planned=pairs_planned,
            ranking_pairs_scored=pairs_scored,
            ranking_pairs_missing=pairs_planned - pairs_scored,
            ranking_accuracy=ranking_accuracy,
            invariance_pairs_planned=inv_planned,
            invariance_violations=inv_violations,
            invariance_violation_rate=(
                inv_violations / inv_planned if inv_planned else None
            ),
            coverage=(completed / planned) if planned else 0.0,
            component_mask_distribution=mask_distribution,
            partial_case_count=partial,
            all_failed_case_count=all_failed,
            limitations=[
                f"finite suite: {planned} cases, {pairs_planned} ranking pairs; "
                "passing does not establish generalization beyond these cases",
                "checkers cover only the declared task contract; behaviour outside "
                "it is untested",
            ],
        )
        return metrics, error_categories

    def _infrastructure_failures(self, results: list[ScoreResult]) -> list[str]:
        out: list[str] = []
        for result in results:
            for component in result.component_results:
                err = component.error
                if err is None:
                    continue
                if err.code in INFRASTRUCTURE_CODES or err.retry_owner == RetryOwner.HARNESS:
                    out.append(f"{result.example_id}:{component.id}:{err.code}")
        return out

    def _decide(
        self,
        metrics: ValidationMetrics,
        policy: AcceptancePolicy,
        infra: list[str],
    ) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if infra:
            reasons.append(
                f"{len(infra)} component execution(s) failed for environmental reasons; "
                "the suite did not complete, so eligibility cannot be established"
            )
        if metrics.planned_cases == 0:
            reasons.append("no development cases were run")
        if metrics.coverage < policy.min_coverage:
            reasons.append(
                f"coverage {metrics.coverage:.3f} < required {policy.min_coverage:.3f}"
            )
        if not policy.allow_partial_component_mask and metrics.partial_case_count:
            reasons.append(
                f"{metrics.partial_case_count} case(s) scored with a partial component "
                "mask and this policy does not admit partial masks"
            )
        for name, value, limit, worse in (
            ("execution_error_rate", metrics.execution_error_rate, policy.max_execution_error_rate, "above"),
            ("false_accept_rate", metrics.false_accept_rate, policy.max_false_accept_rate, "above"),
            ("false_reject_rate", metrics.false_reject_rate, policy.max_false_reject_rate, "above"),
            (
                "invariance_violation_rate",
                metrics.invariance_violation_rate,
                policy.max_invariance_violation_rate,
                "above",
            ),
        ):
            if value is None:
                if name in ("false_accept_rate", "false_reject_rate"):
                    reasons.append(f"{name} is not measurable with this suite")
                continue
            if value > limit + 1e-12:
                reasons.append(f"{name} {value:.3f} is {worse} the limit {limit:.3f}")
        if metrics.ranking_accuracy is None:
            if policy.min_ranking_accuracy > 0:
                reasons.append("ranking accuracy is not measurable with this suite")
        elif metrics.ranking_accuracy < policy.min_ranking_accuracy - 1e-12:
            reasons.append(
                f"ranking_accuracy {metrics.ranking_accuracy:.3f} < required "
                f"{policy.min_ranking_accuracy:.3f} "
                f"({metrics.ranking_pairs_missing} pair(s) could not be scored and "
                "count as failures)"
            )
        return (not reasons), reasons

    # -- integrity --------------------------------------------------------
    def _body_digest(self, report: ValidationReport) -> str:
        body = report.model_dump(mode="json")
        body.pop("integrity_tag", None)
        return hashlib.sha256(canonical_json(body)).hexdigest()

    def sign(self, report: ValidationReport) -> ValidationReport:
        tag = hmac.new(
            self._secret, self._body_digest(report).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return report.model_copy(update={"integrity_tag": tag})

    def verify(self, report: ValidationReport) -> bool:
        """True only for a report this evaluator issued and nobody edited."""
        if not report.integrity_tag:
            return False
        expected = hmac.new(
            self._secret, self._body_digest(report).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, report.integrity_tag)
