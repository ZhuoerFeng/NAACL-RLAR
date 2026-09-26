"""Structured error taxonomy shared by every layer of the harness.

The reliability protocol requires that every failure carries an explicit
``retry_owner`` and an ``action_outcome`` that distinguishes "definitely did not
happen" (``failed``) from "we cannot confirm what the server did" (``unknown``).
Nothing in the harness is allowed to collapse those two into one state.
"""

from __future__ import annotations

from enum import Enum


class RetryOwner(str, Enum):
    """Who is responsible for making progress after this error."""

    AGENT = "agent"  # the model must revise its reward draft / action
    HARNESS = "harness"  # transient; retry the same immutable action
    NONE = "none"  # unrecoverable, budget exhaustion, or operator action needed


class ActionOutcome(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    UNKNOWN = "unknown"


class ErrorCategory(str, Enum):
    PROTOCOL = "protocol"  # bad action JSON, unknown tool, schema violation
    REWARD_CODE = "reward_code"  # syntax / entrypoint / return-type problems
    QUALITY = "quality"  # the reward runs but fails development acceptance
    TRANSPORT = "transport"  # 429, transient 5xx, connection loss
    TIMEOUT = "timeout"
    AUTH = "auth"
    RESOURCE = "resource"  # worker unavailable, disk, output limits
    BUDGET = "budget"
    CANCELLED = "cancelled"
    INPUT = "input"  # malformed record, duplicate id
    INTERNAL = "internal"


# Stable error codes. Kept as plain constants so they can be asserted in tests
# and referenced from trace analysis without importing the enum machinery.
class Code:
    # protocol
    INVALID_ACTION_JSON = "invalid_action_json"
    INVALID_ACTION_SCHEMA = "invalid_action_schema"
    UNKNOWN_TOOL = "unknown_tool"
    TRUNCATED_OUTPUT = "truncated_output"
    BATCH_RULE_VIOLATION = "batch_rule_violation"
    STALE_HISTORY_CURSOR = "stale_history_cursor"
    EMPTY_ACTION_BATCH = "empty_action_batch"
    DUPLICATE_ACTION_ID = "duplicate_action_id"

    # reward code / definition
    DEFINITION_SCHEMA_INVALID = "definition_schema_invalid"
    COMPONENT_SYNTAX_ERROR = "component_syntax_error"
    COMPONENT_IMPORT_ERROR = "component_import_error"
    COMPONENT_MISSING_ENTRYPOINT = "component_missing_entrypoint"
    COMPONENT_RETURN_TYPE = "component_return_type"
    COMPONENT_NON_FINITE = "component_non_finite"
    COMPONENT_OUT_OF_RANGE = "component_out_of_range"
    COMPONENT_BOOL_REJECTED = "component_bool_rejected"
    COMPONENT_RAISED = "component_raised"
    COMPONENT_TIMEOUT = "component_timeout"
    COMPONENT_OUTPUT_LIMIT = "component_output_limit"
    FORBIDDEN_API = "forbidden_api"

    # validation / submission
    NO_DEV_SUITE = "no_dev_suite"
    NOT_ELIGIBLE = "not_eligible"
    REPORT_NOT_FOUND = "report_not_found"
    REPORT_MISMATCH = "report_mismatch"
    REPORT_SUPERSEDED = "report_superseded"
    APPLICABILITY_REJECTED = "applicability_rejected"

    # transport / infra
    HTTP_RATE_LIMITED = "http_rate_limited"
    HTTP_SERVER_ERROR = "http_server_error"
    HTTP_BAD_RESPONSE = "http_bad_response"
    CONNECTION_LOST = "connection_lost"
    DISPATCHED_RESULT_UNKNOWN = "dispatched_result_unknown"
    AUTH_FAILED = "auth_failed"
    WORKER_UNAVAILABLE = "worker_unavailable"
    RUNNER_CAPABILITY_MISSING = "runner_capability_missing"

    # broker
    MODEL_NOT_IN_CATALOGUE = "model_not_in_catalogue"
    SCORING_BUDGET_EXHAUSTED = "scoring_budget_exhausted"
    SCORING_SERVICE_ERROR = "scoring_service_error"

    # budget / termination
    BUDGET_EXHAUSTED = "budget_exhausted"
    CONTEXT_BUDGET_EXHAUSTED = "context_budget_exhausted"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    NO_PROGRESS = "no_progress"

    # input
    MALFORMED_RECORD = "malformed_record"
    DUPLICATE_QUERY_ID = "duplicate_query_id"

    # storage
    JOURNAL_CORRUPT = "journal_corrupt"
    BLOB_MISSING = "blob_missing"
    RUN_DIR_LOCKED = "run_dir_locked"
    WRITE_FAILED = "write_failed"
    INPUT_CHANGED = "input_changed"

    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


class HarnessError(Exception):
    """Base class for harness-side failures that must stop or pause a run."""

    category = ErrorCategory.INTERNAL
    code = Code.INTERNAL_ERROR
    retry_owner = RetryOwner.NONE

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class ConfigError(HarnessError):
    category = ErrorCategory.INPUT


class StorageError(HarnessError):
    category = ErrorCategory.RESOURCE


class JournalCorruptError(StorageError):
    code = Code.JOURNAL_CORRUPT


class BlobMissingError(StorageError):
    code = Code.BLOB_MISSING


class RunDirLockedError(StorageError):
    code = Code.RUN_DIR_LOCKED


class BudgetExhausted(HarnessError):
    category = ErrorCategory.BUDGET
    code = Code.BUDGET_EXHAUSTED


class DeadlineExceeded(HarnessError):
    category = ErrorCategory.BUDGET
    code = Code.DEADLINE_EXCEEDED


class AuthError(HarnessError):
    category = ErrorCategory.AUTH
    code = Code.AUTH_FAILED


class CancelledError(HarnessError):
    category = ErrorCategory.CANCELLED
    code = Code.CANCELLED


class PreflightError(ConfigError):
    """Raised when preflight finds missing capabilities or configuration."""

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.problems = problems or []
