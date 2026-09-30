"""Thin synchronous adapter for verl's custom reward callback.

No verl, torch or Ray dependency is needed here. The adapter converts inputs
and successful results; it does not synthesize rewards or implement scoring
logic, judge clients, retries, reward routing or a framework error protocol.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import partial
from math import isfinite
from threading import Lock
from typing import Any

from ..evaluation.evaluator import ExecutionLimits
from ..evaluation.scoring import score_reward
from ..runtime.runner import Runner
from ..schemas import RewardDefinition, ScoreResult, TaskPack

Scorer = Callable[[dict[str, Any]], ScoreResult]
InputMapper = Callable[[str, str, Any, Mapping[str, Any] | None], dict[str, Any]]


def verl_example(data_source, solution_str, ground_truth, extra_info=None):
    """Map explicit task context; never infer the query or extract an answer.

    Only extra_info.rlar.query and .metadata are scoring inputs. Dataset IDs,
    reward IDs, tool settings and rollout scores are not forwarded. A caller
    with another dataset layout can supply its own input_mapper.
    """
    if not isinstance(solution_str, str):
        raise TypeError('solution_str must be a string')
    if not isinstance(extra_info, Mapping) or not isinstance(extra_info.get('rlar'), Mapping):
        raise ValueError('extra_info.rlar must contain the original query')
    context = extra_info['rlar']
    query = context.get('query')
    if not isinstance(query, str) or not query:
        raise ValueError('extra_info.rlar.query must be a nonempty string')
    metadata = context.get('metadata', {})
    if not isinstance(metadata, dict):
        raise TypeError('extra_info.rlar.metadata must be an object')
    # Existing task packs may allow individual metadata fields at the top level.
    # Unlike arbitrary **metadata expansion, reserved inputs cannot be replaced.
    if set(metadata) & {'query', 'response', 'reference', 'metadata'}:
        raise ValueError('metadata must not redefine reserved scoring inputs')
    return deepcopy({
        **metadata,
        'query': query,
        'response': solution_str,
        'reference': ground_truth,
        'metadata': metadata,
    })


class VerlRewardAdapter:
    """Bind a configured harness scorer to the standard verl entry point.

    A scorer can be an application's durable scoring callable or one created
    with from_reward. Calls on one adapter are serialized because a runner's
    judge service may hold mutable execution bindings. Create it once per
    worker; do not share its runner through another adapter concurrently.
    """

    def __init__(self, scorer: Scorer, *, input_mapper: InputMapper = verl_example):
        self._scorer = scorer
        self._input_mapper = input_mapper
        self._lock = Lock()

    @classmethod
    def from_reward(
        cls,
        definition: RewardDefinition,
        task_pack: TaskPack,
        runner: Runner,
        *,
        limits: ExecutionLimits | None = None,
        input_mapper: InputMapper = verl_example,
    ) -> VerlRewardAdapter:
        """Bind one selected reward, retaining its normalizations and context.

        Selection and validation of the artifact's applicability are owned by
        the application. Rubric rewards use the caller's configured runner and
        judge service; no model or service is selected by this adapter.
        """
        scorer = partial(
            score_reward,
            definition.model_copy(deep=True),
            scoring_context=task_pack.model_copy(deep=True),
            runner=runner,
            limits=deepcopy(limits),
        )
        return cls(scorer, input_mapper=input_mapper)

    def compute_score(
        self,
        data_source: str,
        solution_str: str,
        ground_truth: Any,
        extra_info: Mapping[str, Any] | None = None,
        **runtime_kwargs: Any,
    ) -> dict[str, float]:
        """Return only {'score': total_score}; propagate local failures.

        verl may add reward_router_address/reward_model_tokenizer. Those
        framework objects are deliberately not passed to generated code.
        """
        with self._lock:
            example = self._input_mapper(data_source, solution_str, ground_truth, extra_info)
            result = self._scorer(example)
        value = result.total_score
        if result.status != 'ok':
            raise ValueError(f'verl adapter requires a complete reward result; got {result.status}')
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError('verl adapter requires a finite numeric total_score')
        return {'score': float(value)}
