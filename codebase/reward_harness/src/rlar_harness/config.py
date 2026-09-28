"""Run configuration, preflight and the run manifest.

Every budget and threshold must be stated explicitly and ends up in the run
manifest and the config digest. A real-service template uses the literal
sentinel ``REQUIRED`` for values the operator must supply; preflight reports
*all* of them at once and refuses to fall back to demo thresholds.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from .errors import ConfigError, PreflightError
from .schemas import (
    MANIFEST_SCHEMA_VERSION,
    AcceptancePolicy,
    Assurance,
    RewardMode,
    Strict,
)
from .storage.canonical import digest

REQUIRED = "REQUIRED"


def _collect_required(value: Any, path: str, out: list[str]) -> None:
    if isinstance(value, str) and value == REQUIRED:
        out.append(path)
    elif isinstance(value, dict):
        for k, v in value.items():
            _collect_required(v, f"{path}.{k}", out)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _collect_required(v, f"{path}[{i}]", out)


# --------------------------------------------------------------------------
# config sections
# --------------------------------------------------------------------------


class DataConfig(Strict):
    input_adapter: Literal["jsonl", "parquet"] = "jsonl"
    input_path: str | None = None
    #: Only used by the parquet adapter; explicit, never guessed.
    column_mapping: dict[str, str] = Field(default_factory=dict)
    batch_size: int = 256
    task_pack_root: str
    split: str = "train"
    output_layout: Literal["inline", "reference"] = "inline"
    #: ``strict`` recomputes the input digest on resume and refuses to continue
    #: a run whose input file changed.
    input_digest_policy: Literal["strict"] = "strict"


class ConstructionConfig(Strict):
    strategy: Literal["agentic", "single_completion"] = "agentic"
    reward_mode: RewardMode = "checklist"
    allow_row_mode_override: bool = False
    on_pass: Literal["submit", "return"] = "submit"
    #: Deterministic tie-break used when one batch yields several eligible
    #: candidates. Never "whichever returned first".
    candidate_selector: Literal["best_metric_then_reward_key"] = "best_metric_then_reward_key"
    selector_metric: str = "false_accept_rate"
    seed: int = 0
    reuse_enabled: bool = True
    library_mode: Literal["heldout", "continual"] = "heldout"
    max_components: int = 8


class ModelConfig(Strict):
    provider_adapter: Literal["scripted", "http_chat_json_v1"] = "scripted"
    scripted_responses: str | None = None
    endpoint: str | None = None
    model: str = REQUIRED
    revision: str | None = None
    api_key_env: str | None = None
    temperature: float = 0.0
    send_temperature: bool = True
    max_output_tokens: int = 2048
    max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    enable_thinking: bool | None = None
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    response_format: Literal["json_object"] | None = None
    prompt_cache_key: str | None = None
    context_limit_tokens: int = 32768
    token_counter: Literal["chars_div4_conservative", "adapter"] = "chars_div4_conservative"
    connect_timeout_s: float = 10.0
    read_timeout_s: float = 60.0
    total_timeout_s: float = 120.0
    adapter_version: str = "http_chat_json_v1"
    #: Reserved headroom for the maximum output plus one bounded observation.
    context_reserve_tokens: int = 4096

    @model_validator(mode="after")
    def _check(self) -> "ModelConfig":
        if self.provider_adapter == "http_chat_json_v1" and not self.endpoint:
            raise ValueError("http_chat_json_v1 requires an explicit endpoint URL")
        return self


class ExecutionConfig(Strict):
    runner_type: Literal["subprocess"] = "subprocess"
    runner_profile: Literal["prototype", "formal"] = "prototype"
    #: Capabilities preflight must find on the runner. Formal audit requires
    #: filesystem/network/label isolation; the subprocess runner cannot provide
    #: them and will be rejected.
    required_capabilities: list[str] = Field(default_factory=list)
    runtime_fingerprint: str = "py3.11-stdlib-v1"
    wall_timeout_s: float = 5.0
    cpu_timeout_s: float = 5.0
    memory_limit_mb: int | None = 512
    max_processes: int | None = None
    max_output_bytes: int = 65536
    max_return_bytes: int = 262144
    #: Running arbitrary model-generated code needs this to be true, or an
    #: isolated runner. Bundled fixtures run without it.
    allow_untrusted_code: bool = False
    max_concurrent_actions: int = 4


class RMCatalogueEntry(Strict):
    model_id: str
    revision: str
    model_card_ref: str
    supported_inputs: list[str]
    score_semantics: str
    normalization_id: str
    endpoint_ref: str
    api_key_env: str | None = None
    timeout_s: float = 30.0
    max_calls_per_score: int = 2


class RMConfig(Strict):
    enabled: bool = False
    catalogue: list[RMCatalogueEntry] = Field(default_factory=list)
    normalization_mappings: dict[str, dict[str, Any]] = Field(default_factory=dict)
    max_scoring_requests_per_run: int | None = None


class RetryConfig(Strict):
    max_transport_attempts: int = 3
    backoff_initial_s: float = 0.01
    backoff_multiplier: float = 2.0
    backoff_max_s: float = 1.0
    #: Consecutive infrastructure failures across queries before the whole run
    #: pauses rather than burning the dataset budget.
    circuit_breaker_threshold: int = 3


class BudgetConfig(Strict):
    run: dict[str, float | None] = Field(default_factory=dict)
    episode: dict[str, float | None] = Field(default_factory=dict)
    retry: RetryConfig = Field(default_factory=RetryConfig)
    no_progress_threshold: int = 3
    #: Absolute UTC epoch seconds. ``None`` disables the wall deadline.
    absolute_deadline_utc: float | None = None
    #: Relative convenience for demos; converted to absolute at run start.
    wall_deadline_s: float | None = None
    max_attempts_per_query: int = 1
    per_tool_timeout_s: float = 30.0


class ValidationConfig(Strict):
    dev_suite_root: str
    oracle_version: str
    acceptance_policy_override: AcceptancePolicy | None = None
    #: Audit lives in its own namespace; these paths never reach the controller.
    audit_suite_root: str | None = None
    audit_required_assurance: Assurance = "isolated"
    #: Secret used to tag trusted evaluator reports so a hand-written report
    #: cannot be passed off as evidence.
    evaluator_integrity_env: str | None = None


class LoggingConfig(Strict):
    max_observation_chars: int = 4000
    max_blob_bytes: int = 4_000_000
    message_format_version: str = "harness_observation.v1"
    single_writer_lock: bool = True
    truncation_marker: str = "\n...[truncated]..."


class HarnessConfig(Strict):
    schema_version: Literal["rlar.config.v1"] = "rlar.config.v1"
    run_label: str = "run"
    data: DataConfig
    construction: ConstructionConfig = Field(default_factory=ConstructionConfig)
    model: ModelConfig
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    rm: RMConfig = Field(default_factory=RMConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    validation: ValidationConfig
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    #: Marks demo/prototype configurations so no report can claim otherwise.
    profile_kind: Literal["offline_demo", "real"] = "offline_demo"

    @model_validator(mode="after")
    def validate_limits(self):
        from .budget import DIMENSIONS
        import math
        for scope, limits in (("run", self.budget.run), ("episode", self.budget.episode)):
            if set(limits) != set(DIMENSIONS):
                raise ValueError(f"{scope} budget must explicitly declare {list(DIMENSIONS)}; null means uncapped")
            if any(v is not None and (not math.isfinite(v) or v < 0) for v in limits.values()):
                raise ValueError("budget values must be finite and nonnegative or null")
        if self.budget.episode["controller_steps"] is None:
            raise ValueError("episode controller_steps must be finite")
        positive = [self.model.max_output_tokens, self.model.context_limit_tokens,
            self.model.total_timeout_s, self.model.connect_timeout_s, self.model.read_timeout_s,
            self.execution.wall_timeout_s, self.execution.cpu_timeout_s,
            self.execution.max_output_bytes, self.execution.max_return_bytes,
            self.execution.max_concurrent_actions, self.construction.max_components,
            self.budget.retry.max_transport_attempts, self.budget.no_progress_threshold,
            self.budget.per_tool_timeout_s, self.logging.max_blob_bytes, self.logging.max_observation_chars]
        if any(not math.isfinite(v) or v <= 0 for v in positive):
            raise ValueError("timeouts, context and execution limits must be finite and positive")
        if not self.logging.single_writer_lock:
            raise ValueError("single-writer locking cannot be disabled")
        if self.construction.selector_metric not in ("false_accept_rate", "false_reject_rate", "ranking_accuracy", "coverage", "execution_error_rate"):
            raise ValueError("unknown deterministic selector metric")
        return self

    # -- derived --------------------------------------------------------
    def digest(self) -> str:
        return digest(self.model_dump(mode="json"))

    def missing_required(self) -> list[str]:
        out: list[str] = []
        _collect_required(self.model_dump(mode="json"), "config", out)
        return out


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def load_config(path: str | Path) -> HarnessConfig:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)  # safe_load only: no arbitrary object construction
    if not isinstance(data, dict):
        raise ConfigError(f"config root must be a mapping, got {type(data).__name__}")
    missing = []
    _collect_required(data, "config", missing)
    if missing:
        raise PreflightError("missing required configuration values: " + ", ".join(missing), missing)
    try:
        config = HarnessConfig.model_validate(data)
    except Exception as exc:  # pydantic ValidationError
        raise ConfigError(f"invalid config {path}: {exc}") from exc
    return config


def resolve_paths(config: HarnessConfig, base_dir: Path) -> dict[str, Path]:
    """Resolve configured paths against the config's directory, not the CWD."""

    def resolve(value: str | None) -> Path | None:
        if value is None or value == REQUIRED:
            return None
        p = Path(value)
        return p if p.is_absolute() else (base_dir / p).resolve()

    return {
        "input_path": resolve(config.data.input_path),
        "task_pack_root": resolve(config.data.task_pack_root),
        "dev_suite_root": resolve(config.validation.dev_suite_root),
        "audit_suite_root": resolve(config.validation.audit_suite_root),
    }


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------


class PreflightReport(Strict):
    ok: bool
    problems: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    resolved_paths: dict[str, str | None] = Field(default_factory=dict)
    runner_capabilities: dict[str, Any] = Field(default_factory=dict)
    config_digest: str = ""
    profile_kind: str = "offline_demo"


def preflight(
    config: HarnessConfig,
    base_dir: Path,
    *,
    runner_capabilities: dict[str, Any] | None = None,
    for_audit: bool = False,
) -> PreflightReport:
    """Check everything at once and report every problem found."""
    problems: list[str] = []
    warnings: list[str] = []

    for path in config.missing_required():
        problems.append(f"missing required configuration value: {path}")

    paths = resolve_paths(config, base_dir)
    if config.data.input_adapter == "jsonl":
        if paths["input_path"] is None:
            warnings.append("data.input_path not set; --input must be passed on the CLI")
        elif not paths["input_path"].exists():
            problems.append(f"input file does not exist: {paths['input_path']}")
    if config.data.input_adapter == "parquet":
        problems.append(
            "parquet input adapter is a P1 feature and is not implemented in this build"
        )

    for key in ("task_pack_root", "dev_suite_root"):
        p = paths[key]
        if p is None:
            problems.append(f"{key} is not configured")
        elif not p.exists():
            problems.append(f"{key} does not exist: {p}")

    if paths["task_pack_root"] and paths["task_pack_root"].exists() and paths["dev_suite_root"]:
        from .evaluation.taskpack import TaskPackStore, DevSuiteStore
        store = TaskPackStore(paths["task_pack_root"])
        suites = DevSuiteStore(paths["dev_suite_root"])
        for profile in store.available():
            try:
                pack = store.get(profile)
                if pack.applicability_rule.runtime_fingerprint != config.execution.runtime_fingerprint:
                    problems.append(f"{profile}: runtime fingerprint differs from execution config")
                if pack.verifier_version != config.validation.oracle_version:
                    problems.append(f"{profile}: verifier version differs from configured oracle")
                if pack.dev_suite_ref:
                    suite = suites.get(pack.dev_suite_ref)
                    ids = [case.case_id for case in suite.cases]
                    if len(set(ids)) != len(ids):
                        problems.append(f"{profile}: duplicate development case IDs")
                if config.construction.reward_mode not in pack.mode_constraints:
                    problems.append(f"{profile}: configured reward mode is not permitted")
            except (ConfigError, ValueError) as exc:
                problems.append(f"{profile}: {exc}")

    if config.model.provider_adapter == "http_chat_json_v1":
        if not config.model.endpoint:
            problems.append("model.endpoint is required for the http adapter")
        if config.model.api_key_env and not os.environ.get(config.model.api_key_env):
            problems.append(
                f"model.api_key_env={config.model.api_key_env!r} is not set in the environment"
            )

    if config.rm.enabled:
        seen: set[str] = set()
        for entry in config.rm.catalogue:
            if entry.model_id in seen:
                problems.append(f"duplicate RM catalogue model_id: {entry.model_id}")
            seen.add(entry.model_id)
            if entry.api_key_env and not os.environ.get(entry.api_key_env):
                warnings.append(
                    f"RM {entry.model_id}: {entry.api_key_env} not set; "
                    "scoring calls will fail at dispatch"
                )
        if not config.rm.catalogue:
            problems.append("rm.enabled=true but the catalogue is empty")

    caps = runner_capabilities or {}
    for required in config.execution.required_capabilities:
        if not caps.get(required):
            problems.append(
                f"runner does not provide required capability {required!r} "
                f"(runner reports {caps.get('runner_type', 'unknown')})"
            )

    if for_audit:
        needed = ("filesystem_isolation", "network_isolation", "label_secret_isolation")
        missing = [c for c in needed if not caps.get(c)]
        if missing:
            problems.append(
                "formal audit requires an isolated runner; this backend does not "
                f"provide {missing}. Use --allow-prototype-audit to record an "
                "explicitly prototype-labelled result instead."
            )
        if config.validation.audit_suite_root is None:
            problems.append("validation.audit_suite_root is required for audit")

    if config.execution.runner_profile == "formal" and caps:
        if caps.get("max_assurance") != "isolated":
            problems.append(
                "execution.runner_profile='formal' but the runner's max_assurance is "
                f"{caps.get('max_assurance')!r}"
            )

    if config.profile_kind == "offline_demo":
        warnings.append(
            "profile_kind=offline_demo: thresholds and budgets are demo values; "
            "results are labelled prototype and are not evidence of real task quality"
        )

    return PreflightReport(
        ok=not problems,
        problems=problems,
        warnings=warnings,
        resolved_paths={k: (str(v) if v else None) for k, v in paths.items()},
        runner_capabilities=caps,
        config_digest=config.digest(),
        profile_kind=config.profile_kind,
    )


def require_preflight(report: PreflightReport) -> None:
    if not report.ok:
        raise PreflightError(
            "preflight failed with %d problem(s)" % len(report.problems), report.problems
        )


# --------------------------------------------------------------------------
# run manifest
# --------------------------------------------------------------------------


class RunManifest(Strict):
    schema_version: Literal["rlar.manifest.v1"] = MANIFEST_SCHEMA_VERSION
    run_id: str
    created_at: str
    config: HarnessConfig
    config_digest: str
    input_path: str | None
    input_digest: str | None
    input_record_count: int | None = None
    split: str
    seed: int
    deadline_utc: float | None
    harness_version: str
    platform: str
    runner_capabilities: dict[str, Any] = Field(default_factory=dict)
    profile_kind: str = "offline_demo"
    #: Recorded when a run is created because its predecessor's input changed.
    resolved_paths: dict[str, str | None] = Field(default_factory=dict)
    resource_fingerprints: dict[str, str] = Field(default_factory=dict)
    initial_library_snapshot: str = ""
    supersedes_run_id: str | None = None
    supersedes_reason: str | None = None
