"""Trusted-side validation, normalization and aggregation.

Generated code never reports its own success. The worker hands back whatever
the component returned; everything below runs in the harness process:

* return-type and finiteness checks (``bool`` is rejected unless the ABI opts
  in, ``NaN``/``Inf`` are always rejected),
* the frozen normalization mapping declared by the component — only declared
  mapping IDs, so an arbitrary output scale cannot stand in for a weight,
* the single fixed aggregation rule.

Aggregation (author-confirmed, not configurable):

    single:     s = s_1                          when it executed
    checklist:  s = sum(s_i for i in V) / |V|    V = components that executed
    all failed: s = null, and the whole score is ``failed``

An error is not a zero. Components that all legitimately return 0 give a valid
total of 0.0, which is ``ok``, not ``failed``.
"""

from __future__ import annotations

from typing import Any
import json

from ..errors import ActionOutcome, Code, ErrorCategory, RetryOwner
from ..schemas import (
    AGGREGATION_VERSION,
    ComponentResult,
    RewardDefinition,
    ScoreResult,
    StructuredError,
    Usage,
)


def _err(code: str, message: str) -> StructuredError:
    return StructuredError(
        category=ErrorCategory.REWARD_CODE,
        code=code,
        phase="scoring",
        retry_owner=RetryOwner.AGENT,
        action_outcome=ActionOutcome.COMPLETED,
        message=message,
        suggested_recovery="return a finite float in [0, 1] from score(example, context)",
    )


def coerce_raw_value(raw: Any, *, allow_bool: bool) -> tuple[float | None, StructuredError | None]:
    """Validate the component's return value. Returns ``(value, error)``."""
    if isinstance(raw, dict):
        if "__nonfinite__" in raw:
            return None, _err(
                Code.COMPONENT_NON_FINITE,
                f"score returned a non-finite value ({raw['__nonfinite__']})",
            )
        if "__unsupported_type__" in raw:
            return None, _err(
                Code.COMPONENT_RETURN_TYPE,
                f"score returned an unsupported type: {raw['__unsupported_type__']}",
            )
        return None, _err(Code.COMPONENT_RETURN_TYPE, "score returned a dict")
    if isinstance(raw, bool):
        if not allow_bool:
            return None, _err(
                Code.COMPONENT_BOOL_REJECTED,
                "score returned a bool; the 'v1' scoring ABI requires a float in "
                "[0, 1]. Use the 'v1+bool' ABI to opt in to bool->0/1.",
            )
        return (1.0 if raw else 0.0), None
    if isinstance(raw, (int, float)):
        value = float(raw)
        if value != value or value in (float("inf"), float("-inf")):
            return None, _err(Code.COMPONENT_NON_FINITE, "score returned a non-finite value")
        return value, None
    if raw is None:
        return None, _err(Code.COMPONENT_RETURN_TYPE, "score returned None")
    return None, _err(
        Code.COMPONENT_RETURN_TYPE, f"score returned {type(raw).__name__}, expected float"
    )


def apply_normalization(
    value: float,
    normalization,
    mappings: dict[str, dict[str, Any]],
) -> tuple[float | None, StructuredError | None]:
    if normalization.kind == "identity":
        if not (0.0 <= value <= 1.0):
            return None, _err(
                Code.COMPONENT_OUT_OF_RANGE,
                f"score {value} is outside [0, 1] and the component declared "
                "identity normalization",
            )
        return value, None

    spec = mappings.get(normalization.mapping_id or "")
    if spec is None:
        return None, _err(
            Code.COMPONENT_OUT_OF_RANGE,
            f"normalization mapping {normalization.mapping_id!r} is not declared by "
            "this profile; only declared mappings may be referenced",
        )
    kind = spec.get("kind", "linear")
    if kind == "linear":
        low = float(spec["source_range"][0])
        high = float(spec["source_range"][1])
        if high == low:
            return None, _err(
                Code.COMPONENT_OUT_OF_RANGE,
                f"mapping {normalization.mapping_id!r} has a degenerate source range",
            )
        scaled = (value - low) / (high - low)
        if spec.get("clamp", True):
            scaled = min(1.0, max(0.0, scaled))
        if not (0.0 <= scaled <= 1.0):
            return None, _err(
                Code.COMPONENT_OUT_OF_RANGE,
                f"raw score {value} maps outside [0, 1] under "
                f"{normalization.mapping_id!r}",
            )
        return scaled, None
    return None, _err(
        Code.COMPONENT_OUT_OF_RANGE,
        f"unsupported normalization mapping kind {kind!r}",
    )


def finalize_component(
    result: ComponentResult,
    component,
    *,
    allow_bool: bool,
    mappings: dict[str, dict[str, Any]],
    structured: bool = False,
) -> ComponentResult:
    """Turn a runner-level result into a validated, normalized one."""
    if result.status == "error":
        return result.model_copy(update={"raw_value": None})

    value = result.raw_value
    if structured:
        try:
            if not isinstance(value, dict) or set(value) != {'raw_score', 'feedback', 'evidence'}:
                raise ValueError('v2 result requires exactly raw_score, feedback, evidence')
            if not isinstance(value['feedback'], str) or not isinstance(value['evidence'], list) or not all(isinstance(e, dict) for e in value['evidence']):
                raise ValueError('feedback must be text and evidence must be an object list')
            if len(value['feedback']) > 8000 or len(json.dumps(value['evidence'], allow_nan=False).encode()) > 16000:
                raise ValueError('component feedback/evidence exceeds bound')
            result = result.model_copy(update={'feedback': value['feedback'], 'evidence': value['evidence']})
            value = value['raw_score']
        except (ValueError, TypeError) as exc:
            return result.model_copy(update={'status': 'error', 'raw_value': None,
                'error': _err(Code.COMPONENT_RETURN_TYPE, str(exc))})
    raw, error = coerce_raw_value(value, allow_bool=allow_bool)
    if error is not None:
        return result.model_copy(
            update={"status": "error", "error": error, "raw_value": None}
        )

    normalized, error = apply_normalization(raw, component.normalization, mappings)
    if error is not None:
        return result.model_copy(
            update={
                "status": "error",
                "error": error,
                "raw_score": raw,
                "raw_value": None,
            }
        )
    return result.model_copy(
        update={
            "status": "ok",
            "raw_score": raw,
            "score": normalized,
            "normalization_id": component.normalization.mapping_id
            or component.normalization.kind,
            "raw_value": None,
            "error": None,
        }
    )


def aggregate(
    definition: RewardDefinition,
    component_results: list[ComponentResult],
    *,
    reward_key: str,
    mappings: dict[str, dict[str, Any]] | None = None,
    example_id: str | None = None,
    usage: Usage | None = None,
) -> ScoreResult:
    """Validate, normalize and aggregate one example's component results."""
    mappings = mappings or {}
    allow_bool = definition.runtime_contract.scoring_abi == "v1+bool"
    by_id = {c.id: c for c in definition.components}
    if [r.id for r in component_results] != definition.component_ids:
        component_results = [ComponentResult(id=c.id, status='error',
            error=_err(Code.COMPONENT_RETURN_TYPE, 'runner component coverage/order mismatch')) for c in definition.components]

    finalized: list[ComponentResult] = []
    for result in component_results:
        component = by_id.get(result.id)
        if component is None:
            finalized.append(
                result.model_copy(
                    update={
                        "status": "error",
                        "raw_value": None,
                        "error": _err(
                            Code.COMPONENT_RETURN_TYPE,
                            f"result for unknown component {result.id!r}",
                        ),
                    }
                )
            )
            continue
        finalized.append(
            finalize_component(result, component, allow_bool=allow_bool, mappings=mappings,
                               structured=definition.runtime_contract.scoring_abi == "v2")
        )

    planned = len(definition.components)
    valid = [r for r in finalized if r.status == "ok" and r.score is not None]
    valid_ids = [r.id for r in valid]

    if not valid:
        total: float | None = None
        status = "failed"
    else:
        if definition.aggregation.kind == "identity":
            total = valid[0].score
        else:
            total = sum(r.score for r in valid) / len(valid)  # type: ignore[misc]
        status = "ok" if len(valid) == planned else "partial"

    return ScoreResult(
        schema_version="rlar.score.v2" if definition.runtime_contract.scoring_abi == "v2" else "rlar.score.v1",
        status=status,  # type: ignore[arg-type]
        total_score=total,
        component_results=finalized,
        valid_component_ids=valid_ids,
        coverage=(len(valid) / planned) if planned else 0.0,
        planned_count=planned,
        successful_count=len(valid),
        reward_key=reward_key,
        aggregation_version=AGGREGATION_VERSION,
        usage=usage or Usage(),
        example_id=example_id,
    )
