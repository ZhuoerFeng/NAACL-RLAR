"""Versioned data and state protocol for the reward-construction harness.

Every persisted object carries a ``schema_version`` and is validated strictly:
unknown fields are rejected rather than silently ignored, because an ignored
field is how a weighted-aggregation config would quietly become an unweighted
one.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .errors import ActionOutcome, ErrorCategory, RetryOwner

REWARD_SCHEMA_VERSION = "rlar.reward.v1"
RESULT_SCHEMA_VERSION = "rlar.result.v1"
REPORT_SCHEMA_VERSION = "rlar.validation.v1"
SCORE_SCHEMA_VERSION = "rlar.score.v1"
TOOL_SCHEMA_VERSION = "rlar.tool.v1"
TASKPACK_SCHEMA_VERSION = "rlar.taskpack.v1"
LIBRARY_SCHEMA_VERSION = "rlar.library.v1"
EVENT_SCHEMA_VERSION = "rlar.event.v1"
MANIFEST_SCHEMA_VERSION = "rlar.manifest.v1"
CHECKPOINT_SCHEMA_VERSION = "rlar.checkpoint.v1"
SFT_SCHEMA_VERSION = "rlar.sft.v1"

#: Bumped whenever the trusted aggregation semantics change. Reports bound to an
#: older aggregation version are not valid evidence for a new submission.
AGGREGATION_VERSION = "agg.v1"

#: Scoring ABIs understood by the worker. ``v1`` rejects ``bool`` returns;
#: ``v1+bool`` explicitly opts in to ``True/False -> 1.0/0.0``.
SCORING_ABIS = ("v1", "v1+bool")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# --------------------------------------------------------------------------
# usage / cost
# --------------------------------------------------------------------------


class Usage(Strict):
    """Cost accounting. ``None`` means unknown; it never means zero."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    #: Number of usage reports that came back unusable (provider omitted usage,
    #: request outcome unknown, ...). Never silently treated as free.
    unknown_usage_events: int = 0
    controller_logical_calls: int = 0
    physical_requests: int = 0
    transport_retries: int = 0
    scoring_requests: int = 0
    tool_calls: int = 0
    component_executions: int = 0
    test_cases: int = 0
    wall_seconds: float = 0.0
    #: ``None`` when no price table is configured. Explicitly not 0.0.
    cost_usd: float | None = None
    cost_status: Literal["known", "unavailable", "unknown"] = "unavailable"

    def merged(self, other: "Usage") -> "Usage":
        def add(a: int | None, b: int | None) -> int | None:
            if a is None and b is None:
                return None
            return (a or 0) + (b or 0)

        if self.cost_usd is None and other.cost_usd is None:
            cost = None
        else:
            cost = (self.cost_usd or 0.0) + (other.cost_usd or 0.0)
        statuses = {self.cost_status, other.cost_status}
        if "unknown" in statuses:
            cost_status = "unknown"
        elif statuses == {"known"}:
            cost_status = "known"
        elif "known" in statuses:
            cost_status = "unknown"
        else:
            cost_status = "unavailable"
        return Usage(
            input_tokens=add(self.input_tokens, other.input_tokens),
            output_tokens=add(self.output_tokens, other.output_tokens),
            cached_input_tokens=add(self.cached_input_tokens, other.cached_input_tokens),
            unknown_usage_events=self.unknown_usage_events + other.unknown_usage_events,
            controller_logical_calls=self.controller_logical_calls
            + other.controller_logical_calls,
            physical_requests=self.physical_requests + other.physical_requests,
            transport_retries=self.transport_retries + other.transport_retries,
            scoring_requests=self.scoring_requests + other.scoring_requests,
            tool_calls=self.tool_calls + other.tool_calls,
            component_executions=self.component_executions + other.component_executions,
            test_cases=self.test_cases + other.test_cases,
            wall_seconds=self.wall_seconds + other.wall_seconds,
            cost_usd=cost,
            cost_status=cost_status,  # type: ignore[arg-type]
        )


def sum_usage(items) -> Usage:
    total = Usage()
    for item in items:
        total = total.merged(item)
    return total


# --------------------------------------------------------------------------
# input records
# --------------------------------------------------------------------------

RewardMode = Literal["single", "checklist"]


class InputLocation(Strict):
    path: str
    line: int  # 1-based line number within the input stream

    def as_key_suffix(self) -> str:
        return f"{self.path}#L{self.line}"


class QueryRecord(Strict):
    schema_version: Literal["rlar.query.v1"] = "rlar.query.v1"
    query_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    task_profile_id: str = Field(min_length=1)
    reference: Any | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    reward_mode: RewardMode | None = None

    @field_validator("reference")
    @classmethod
    def _json_reference(cls, value):
        _assert_json_compatible(value, "reference")
        return value

    @field_validator("metadata")
    @classmethod
    def _json_only(cls, v: dict[str, Any]) -> dict[str, Any]:
        _assert_json_compatible(v, "metadata")
        return v


def _assert_json_compatible(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            raise ValueError(f"{path}: non-finite numbers are not JSON-compatible")
        return
    if isinstance(value, list):
        for i, item in enumerate(value):
            _assert_json_compatible(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, item in value.items():
            if not isinstance(k, str):
                raise ValueError(f"{path}: object keys must be strings")
            _assert_json_compatible(item, f"{path}.{k}")
        return
    raise ValueError(f"{path}: {type(value).__name__} is not JSON-compatible")


# --------------------------------------------------------------------------
# reward definition
# --------------------------------------------------------------------------

_WEIGHT_LIKE_KEYS = {
    "weight",
    "weights",
    "component_weights",
    "weighted_sum",
    "weighted_mean",
    "coefficient",
    "coefficients",
}


class Normalization(Frozen):
    """Frozen mapping of a component's raw score into [0, 1].

    ``identity`` requires the component to already return a value in [0, 1].
    ``mapping`` refers to a mapping declared by the task pack / RM catalogue;
    the agent may only reference declared mapping IDs, so it cannot smuggle a
    weight in through an arbitrary output scale.
    """

    kind: Literal["identity", "mapping"]
    range: tuple[float, float] = (0.0, 1.0)
    mapping_id: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "Normalization":
        if tuple(self.range) != (0.0, 1.0):
            raise ValueError("normalization.range must be [0, 1]")
        if self.kind == "mapping" and not self.mapping_id:
            raise ValueError("normalization.kind='mapping' requires mapping_id")
        if self.kind == "identity" and self.mapping_id is not None:
            raise ValueError("normalization.kind='identity' must not set mapping_id")
        return self


class Component(Frozen):
    id: str = Field(min_length=1)
    criterion: str = Field(min_length=1)
    source: str = Field(min_length=1)
    entrypoint: Literal["score"] = "score"
    normalization: Normalization
    required_apis: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _reject_weights(cls, data: Any) -> Any:
        _reject_weight_keys(data, "component")
        return data


class Aggregation(Frozen):
    kind: Literal["identity", "mean"]

    @model_validator(mode="before")
    @classmethod
    def _reject_weights(cls, data: Any) -> Any:
        _reject_weight_keys(data, "aggregation")
        return data


def _reject_weight_keys(data: Any, where: str) -> None:
    if isinstance(data, dict):
        offending = sorted(_WEIGHT_LIKE_KEYS.intersection(data.keys()))
        if offending:
            raise ValueError(
                f"{where}: weighted aggregation is not part of this schema; "
                f"remove {offending}. checklist components are always equally weighted."
            )


class RuntimeContract(Frozen):
    """Backend-independent fingerprint of everything that can change scores."""

    scoring_abi: Literal["v1", "v1+bool"] = "v1"
    python_version: str = "3.11+"
    dependencies: list[str] = Field(default_factory=list)
    scoring_sdk: str = "rlar.scoring.v1"
    api_revisions: dict[str, str] = Field(default_factory=dict)
    aggregator_version: str = AGGREGATION_VERSION
    environment_ref: str = "configured_at_run_start"


class RewardDefinition(Frozen):
    schema_version: Literal["rlar.reward.v1"] = REWARD_SCHEMA_VERSION
    mode: RewardMode
    components: list[Component] = Field(min_length=1)
    aggregation: Aggregation
    runtime_contract: RuntimeContract = Field(default_factory=RuntimeContract)

    @model_validator(mode="before")
    @classmethod
    def _reject_weights(cls, data: Any) -> Any:
        _reject_weight_keys(data, "reward_definition")
        return data

    @model_validator(mode="after")
    def _check(self) -> "RewardDefinition":
        ids = [c.id for c in self.components]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate component ids: {dupes}")
        if self.mode == "single":
            if len(self.components) != 1:
                raise ValueError("mode='single' requires exactly one component")
            if self.aggregation.kind != "identity":
                raise ValueError("mode='single' requires aggregation.kind='identity'")
        else:
            if self.aggregation.kind != "mean":
                raise ValueError("mode='checklist' requires aggregation.kind='mean'")
        return self

    @property
    def component_ids(self) -> list[str]:
        return [c.id for c in self.components]


# --------------------------------------------------------------------------
# scoring results
# --------------------------------------------------------------------------

ComponentStatus = Literal["ok", "error"]


class ComponentResult(Strict):
    id: str
    status: ComponentStatus
    #: Exactly what the component returned, JSON-represented so the trusted
    #: side can still tell ``True`` from ``1`` and NaN from a number. Populated
    #: by the runner; validated and cleared into ``raw_score`` by the trusted
    #: aggregator. Non-finite floats arrive as ``{"__nonfinite__": "nan"}`` and
    #: unsupported objects as ``{"__unsupported_type__": "..."}``.
    raw_value: Any = None
    raw_score: float | None = None
    score: float | None = None  # normalized; only this enters aggregation
    normalization_id: str | None = None
    error: "StructuredError | None" = None
    stdout_truncated: bool = False
    wall_seconds: float | None = None


class ScoreResult(Strict):
    schema_version: Literal["rlar.score.v1"] = SCORE_SCHEMA_VERSION
    status: Literal["ok", "partial", "failed"]
    total_score: float | None
    component_results: list[ComponentResult]
    valid_component_ids: list[str]
    coverage: float
    planned_count: int
    successful_count: int
    reward_key: str
    aggregation_version: str = AGGREGATION_VERSION
    usage: Usage = Field(default_factory=Usage)
    example_id: str | None = None


# --------------------------------------------------------------------------
# structured errors / tool envelope
# --------------------------------------------------------------------------


class StructuredError(Strict):
    category: ErrorCategory
    code: str
    phase: str
    retry_owner: RetryOwner
    action_id: str | None = None
    action_outcome: ActionOutcome = ActionOutcome.FAILED
    reward_key: str | None = None
    remaining_budget: dict[str, Any] | None = None
    message: str = ""
    suggested_recovery: str | None = None
    detail_ref: str | None = None  # blob ref to the full untruncated diagnostic


ComponentResult.model_rebuild()


class ToolResult(Strict):
    schema_version: Literal["rlar.tool.v1"] = TOOL_SCHEMA_VERSION
    call_id: str
    tool: str
    #: Whether the *operation* completed. A completed test that reports a bad
    #: reward is ``completed`` with a quality verdict inside ``result``.
    status: Literal["completed", "failed", "unknown"]
    result: dict[str, Any] | None = None
    error: StructuredError | None = None
    usage: Usage = Field(default_factory=Usage)
    trace_ref: str | None = None


# --------------------------------------------------------------------------
# task packs and validation reports
# --------------------------------------------------------------------------


class AcceptancePolicy(Strict):
    """Pre-declared thresholds. Never inferred at runtime."""

    score_threshold: float = 0.5
    tie_tolerance: float = 1e-9
    max_false_accept_rate: float = 0.0
    max_false_reject_rate: float = 0.0
    max_execution_error_rate: float = 0.0
    min_ranking_accuracy: float = 1.0
    max_invariance_violation_rate: float = 0.0
    #: Fraction of planned dev cases that must complete for the run to even be
    #: considered. A ``partial`` execution is never automatically eligible.
    min_coverage: float = 1.0
    #: Whether a partial component mask on any dev case may still be eligible.
    allow_partial_component_mask: bool = False

    @model_validator(mode="after")
    def check_thresholds(self):
        import math
        for name, value in self.model_dump().items():
            if isinstance(value, bool):
                continue
            if not math.isfinite(value) or value < 0 or (name != "tie_tolerance" and value > 1):
                raise ValueError("acceptance thresholds must be finite probabilities; tolerance must be nonnegative")
        return self


class ApplicabilityRule(Strict):
    """Trusted, deterministic reuse rule. Text tags alone are never enough."""

    task_contract_digest: str
    permitted_inputs: list[str]
    mode_constraints: list[RewardMode]
    runtime_fingerprint: str
    #: Optional informational tags. Recorded, but not sufficient for reuse.
    task_tags: list[str] = Field(default_factory=list)


class TaskPack(Strict):
    schema_version: Literal["rlar.taskpack.v1"] = TASKPACK_SCHEMA_VERSION
    profile_id: str
    version: str
    task_contract: str
    applicability_rule: ApplicabilityRule
    permitted_inputs: list[str]
    permitted_apis: list[str]
    mode_constraints: list[RewardMode]
    dev_suite_ref: str | None = None
    verifier_version: str
    acceptance_policy: AcceptancePolicy
    report_policy: dict[str, Any] = Field(default_factory=dict)
    normalization_mappings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_components: int = 8
    #: Resources the controller may pull with ``read_resource``. Audit paths are
    #: held by the evaluator config and never appear here.
    resources: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_applicability(self):
        from .storage.canonical import digest
        if self.applicability_rule.task_contract_digest != digest(self.task_contract):
            raise ValueError("task contract digest does not match its content")
        if self.applicability_rule.permitted_inputs != self.permitted_inputs or self.applicability_rule.mode_constraints != self.mode_constraints:
            raise ValueError("applicability rule does not match profile permissions")
        return self


class DevCase(Strict):
    case_id: str
    kind: Literal["correct", "incorrect", "invariance", "perturbation"]
    example: dict[str, Any]
    #: Oracle label, held by the trusted evaluator and never sent to a worker.
    is_correct: bool
    group_id: str | None = None
    #: For invariance/perturbation cases: the case_id this one is derived from.
    base_case_id: str | None = None


class DevSuite(Strict):
    suite_id: str
    version: str
    cases: list[DevCase] = Field(min_length=1)


class ValidationMetrics(Strict):
    planned_cases: int
    completed_cases: int
    failed_cases: int
    execution_error_rate: float | None
    false_accept_rate: float | None
    false_reject_rate: float | None
    ranking_pairs_planned: int
    ranking_pairs_scored: int
    ranking_pairs_missing: int
    ranking_accuracy: float | None
    invariance_pairs_planned: int
    invariance_violations: int
    invariance_violation_rate: float | None
    coverage: float
    component_mask_distribution: dict[str, int] = Field(default_factory=dict)
    partial_case_count: int = 0
    all_failed_case_count: int = 0
    #: Explicit notes about what these finite tests cannot establish.
    limitations: list[str] = Field(default_factory=list)


Assurance = Literal["static", "behavioral_prototype", "isolated"]


class ValidationReport(Strict):
    schema_version: Literal["rlar.validation.v1"] = REPORT_SCHEMA_VERSION
    report_id: str
    reward_key: str
    task_contract_digest: str
    suite_id: str
    suite_version: str
    suite_digest: str = ""
    verifier_version: str
    runtime_fingerprint: str
    aggregation_version: str = AGGREGATION_VERSION
    profile_id: str
    case_ids: list[str]
    metrics: ValidationMetrics
    per_case: list[ScoreResult] = Field(default_factory=list)
    error_categories: dict[str, int] = Field(default_factory=dict)
    eligible: bool
    ineligibility_reasons: list[str] = Field(default_factory=list)
    assurance: Assurance
    policy: AcceptancePolicy
    usage: Usage = Field(default_factory=Usage)
    created_at: str
    #: Set by the evaluator; a report produced by anything else is not evidence.
    issuer: Literal["trusted_evaluator"] = "trusted_evaluator"
    #: HMAC over the canonical report body, keyed by the run's evaluator secret.
    integrity_tag: str | None = None


# --------------------------------------------------------------------------
# construction results
# --------------------------------------------------------------------------

ConstructionStatus = Literal["success", "unvalidated", "failed", "skipped"]


class ConstructionResult(Strict):
    schema_version: Literal["rlar.result.v1"] = RESULT_SCHEMA_VERSION
    query_id: str
    input_location: InputLocation
    input_digest: str
    run_config_digest: str
    library_snapshot: str
    job_key: str
    episode_id: str
    attempt_id: int
    status: ConstructionStatus
    stop_reason: str
    reward_key: str | None = None
    reward_definition: RewardDefinition | None = None
    validation_ref: str | None = None
    fallback_key: str | None = None
    trace_ref: str
    usage: Usage = Field(default_factory=Usage)
    reused: bool = False
    error: StructuredError | None = None


# --------------------------------------------------------------------------
# library entries
# --------------------------------------------------------------------------


class LibraryEntry(Strict):
    schema_version: Literal["rlar.library.v1"] = LIBRARY_SCHEMA_VERSION
    reward_key: str
    definition: RewardDefinition
    applicability: ApplicabilityRule
    validation_ref: str
    validation_summary: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    committed_at: str
    #: Sequence number of the ``results.jsonl`` commit that published this entry.
    published_by_result_seq: int


# --------------------------------------------------------------------------
# LLM protocol
# --------------------------------------------------------------------------

Role = Literal["system", "user", "assistant"]


class Message(Strict):
    role: Role
    content: str
    #: Harness-side classification used by trace/SFT export. Not sent to the
    #: provider and not part of the prefix digest input.
    actor: Literal["run_prefix", "episode_prefix", "assistant", "harness_observation"]
    trainable: bool = False
    meta: dict[str, Any] = Field(default_factory=dict)


class ProposedAction(Strict):
    id: str
    tool: str
    arguments: dict[str, Any]


class LLMRequest(Strict):
    episode_id: str
    expected_history_cursor: int
    prefix_ref: str
    history_ref: str
    model_config_ref: str
    logical_call_id: str
    remaining_budget: dict[str, Any]
    deadline: float | None = None  # absolute UTC epoch seconds
    max_output_tokens: int
    temperature: float
    #: Materialized messages. Immutable for all physical retries of this request.
    messages: list[Message]


LLMStatus = Literal["complete", "incomplete", "failed", "unknown"]


class LLMResult(Strict):
    status: LLMStatus
    assistant_message_ref: str | None = None
    assistant_text: str | None = None
    proposed_actions: list[ProposedAction] = Field(default_factory=list)
    parse_error: StructuredError | None = None
    request_digest: str = ""
    provider_request_id: str | None = None
    finish_reason: str | None = None
    usage: Usage = Field(default_factory=Usage)
    error: StructuredError | None = None
    trace_ref: str | None = None
    history_cursor: int = 0


# --------------------------------------------------------------------------
# state machine
# --------------------------------------------------------------------------


class RunState(str, Enum):
    NEW = "NEW"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


class EpisodeState(str, Enum):
    NEW = "NEW"
    INPUT_VALIDATED = "INPUT_VALIDATED"
    REUSE_CHECK = "REUSE_CHECK"
    LLM_PENDING = "LLM_PENDING"
    ACTIONS_PENDING = "ACTIONS_PENDING"
    OBSERVATIONS_READY = "OBSERVATIONS_READY"
    SELECTED = "SELECTED"
    UNVALIDATED = "UNVALIDATED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


TERMINAL_EPISODE_STATES = {
    EpisodeState.SELECTED,
    EpisodeState.UNVALIDATED,
    EpisodeState.FAILED,
}


# --------------------------------------------------------------------------
# runner protocol
# --------------------------------------------------------------------------


class RunnerCapabilities(Strict):
    runner_type: str
    #: Truthful declarations. A platform that cannot enforce a limit must say so
    #: here rather than reporting a limit it does not apply.
    filesystem_isolation: bool
    network_isolation: bool
    label_secret_isolation: bool
    memory_limit_enforced: bool
    process_limit_enforced: bool
    cpu_time_limit_enforced: bool
    wall_timeout_enforced: bool
    process_group_cleanup: bool
    max_assurance: Assurance
    platform: str
    notes: list[str] = Field(default_factory=list)


class ExecuteRequest(Strict):
    request_id: str
    action_id: str
    definition: RewardDefinition
    #: Whitelisted scoring inputs only. No oracle labels, no dataset handles.
    examples: list[dict[str, Any]]
    example_ids: list[str]
    runtime_fingerprint: str
    permitted_apis: list[str]
    normalization_mappings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    wall_timeout_s: float
    cpu_timeout_s: float
    memory_limit_mb: int | None = None
    max_output_bytes: int = 65536
    max_return_bytes: int = 262144


class ExecuteResponse(Strict):
    request_id: str
    #: One inner list per entry of ``example_ids``, in the same order, holding
    #: one :class:`ComponentResult` per component of the definition.
    per_example: list[list[ComponentResult]]
    example_ids: list[str]
    status: Literal["completed", "failed", "unknown"]
    error: StructuredError | None = None
    usage: Usage = Field(default_factory=Usage)
    service_request_ids: list[str] = Field(default_factory=list)
