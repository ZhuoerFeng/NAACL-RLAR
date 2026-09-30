"""Shared durable execution and capability-level verification with signed evidence."""
from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..errors import HarnessError, RetryOwner
from ..runtime.aggregate import aggregate
from ..runtime.runner import Runner, make_execute_request
from ..schemas import (
    AGGREGATION_VERSION,
    AcceptancePolicy,
    Assurance,
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
        if pack.reward_logic_policy and definition.runtime_contract.reward_logic_policy != pack.reward_logic_policy:
            raise ValueError('reward logic policy does not match the task pack')
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

    def durable_scores(self, definition, pack, examples, example_ids, *, action_id, reward_key):
        """One persisted execution path; unknown executions consume new bounded attempts."""
        journal, blobs, episode_id = self.persistence
        from ..errors import HarnessError
        from ..durability import fault
        binding = digest({'definition': definition, 'pack': pack, 'examples': examples, 'ids': example_ids})
        def emit(kind, **payload):
            journal.append(kind, {'scoring_id': action_id, 'binding': binding, **payload}, episode_id=episode_id)
        for attempt in range(self.execution_attempts):
            events = [e for e in journal.scan().events if e.episode_id == episode_id
                      and e.payload.get('scoring_id') == action_id and e.payload.get('attempt') == attempt]
            if any(e.payload['binding'] != binding for e in events):
                from ..errors import StorageError
                raise StorageError('execution inputs changed on resume')
            done = next((e for e in events if e.type == 'score_batch_result'), None)
            if done:
                return [ScoreResult.model_validate(r) for r in blobs.get_json(done.payload['scores_ref'])]
            if any(e.type == 'score_batch_dispatched' for e in events):
                emit('score_batch_unknown', attempt=attempt)
                continue
            self.budget.debit_once(f'{action_id}:execute:{attempt}', {
                'component_executions': float(len(examples) * len(definition.components))})
            emit('score_batch_dispatched', attempt=attempt)
            scores, usage = self.score_examples(definition, pack, examples, example_ids,
                action_id=action_id, reward_key=reward_key)
            emit('score_batch_result', attempt=attempt, scores_ref=blobs.put_json([s.model_dump(mode='json') for s in scores]))
            fault('execution_after_evidence')
            return scores
        raise HarnessError('execution interrupted too often', code='environment_unavailable')

    # -- validation ------------------------------------------------------

    def validate(self, definition, pack, suite, *, reward_key, action_id):
        from .suite import assert_frozen, example_input
        from .verifier import base_decision, evidence_for, check_decision
        from ..schemas import ValidationDecision
        from ..durability import fault
        assert_frozen(suite)
        journal, blobs, episode_id = self.persistence
        def events(kind):
            return [e for e in journal.scan().events if e.episode_id == episode_id
                    and e.type == kind and e.payload.get('evaluation_id') == action_id]
        def emit(kind, **payload):
            journal.append(kind, {'evaluation_id': action_id, **payload}, episode_id=episode_id)
        stored = events('execution_evidence')
        if stored:
            scores = [ScoreResult.model_validate(s) for s in blobs.get_json(stored[0].payload['scores_ref'])]
        else:
            scores = self.durable_scores(definition, pack,
                [example_input(e, self.query, pack) for e in suite.examples], [e.id for e in suite.examples],
                action_id=action_id, reward_key=reward_key)
            emit('execution_evidence', scores_ref=blobs.put_json([r.model_dump(mode='json') for r in scores]))
        decisions = []
        for case in suite.cases:
            evidence = evidence_for(case, suite, definition, scores, pack, self.verifier, query=self.query)
            previous = [e for e in events('verification_decision') if e.payload['case_id'] == case.id]
            if previous:
                decision = ValidationDecision.model_validate(blobs.get_json(previous[0].payload['decision_ref']))
            else:
                try:
                    decision = self.verifier.verify(evidence, f'{action_id}:{case.id}')
                except HarnessError as exc:
                    if case.required or exc.code != 'environment_unavailable':
                        raise
                    # Diagnostic availability cannot veto completed required cases.
                    # Keep an explicit decision; durable I/O retains failed/unknown
                    # attempts and their charged budgets for export and recovery.
                    decision = base_decision(evidence, status='error',
                        rationale='diagnostic verifier service unavailable after bounded transport attempts')
                emit('verification_decision', case_id=case.id, evidence_ref=blobs.put_json(evidence),
                     decision_ref=blobs.put_json(decision.model_dump(mode='json')))
                fault('verifier_after_decision')
            check_decision(decision, evidence)
            decisions.append(decision)
        required = {c.id for c in suite.cases if c.required}
        reasons = [f'{d.case_id}:{d.operation_status}:{d.passed}' for d in decisions
                   if d.case_id in required and (d.operation_status != 'completed' or d.passed is not True)]
        completed = sum(d.operation_status == 'completed' for d in decisions)
        rankings = [d for d, case in zip(decisions, suite.cases) if case.kind == 'ranking']
        pairs = sum(len(c.relations) for c in suite.cases)
        from collections import Counter
        masks = Counter(','.join(s.valid_component_ids) for s in scores)
        metrics = ValidationMetrics(planned_cases=len(suite.cases), completed_cases=completed,
            failed_cases=len(suite.cases)-completed, execution_error_rate=None, false_accept_rate=None,
            false_reject_rate=None, ranking_pairs_planned=pairs,
            ranking_pairs_scored=sum(len(c.relations) for c, d in zip(suite.cases, decisions) if d.operation_status == 'completed'),
            ranking_pairs_missing=sum(len(c.relations) for c, d in zip(suite.cases, decisions) if d.operation_status != 'completed'),
            ranking_accuracy=sum(d.passed is True for d in rankings)/len(rankings) if rankings else None,
            invariance_pairs_planned=0, invariance_violations=0, invariance_violation_rate=None,
            coverage=completed/len(suite.cases), component_mask_distribution=dict(masks),
            partial_case_count=sum(s.status == 'partial' for s in scores),
            all_failed_case_count=sum(s.status == 'failed' for s in scores),
            limitations=['Development acceptance is not independent task quality or production isolation.'])
        report = ValidationReport(schema_version='rlar.validation.v2', report_id=digest({'evaluation': action_id, 'reward': reward_key}),
            reward_key=reward_key, task_contract_digest=pack.applicability_rule.task_contract_digest,
            suite_id=suite.id, suite_version=suite.version, suite_digest=suite.suite_digest,
            verifier_version=pack.verifier_version, runtime_fingerprint=pack.applicability_rule.runtime_fingerprint,
            profile_id=pack.profile_id, case_ids=[c.id for c in suite.cases], metrics=metrics,
            per_case=scores, eligible=bool(required) and not reasons, ineligibility_reasons=reasons,
            assurance=self.assurance(), policy=pack.acceptance_policy, decisions=decisions,
            evidence_digest=digest(scores), verifier_config_digest=self.verifier.config_digest,
            created_at=datetime.now(timezone.utc).isoformat())
        return ValidationOutcome(self.sign(report), self._infrastructure_failures(scores), scores)


    # -- metrics ---------------------------------------------------------

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
