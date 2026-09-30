"""Framework-independent execution of an already constructed reward."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from ..runtime.runner import Runner
from ..schemas import RewardDefinition, ScoreResult, TaskPack
from ..storage.canonical import reward_key_for
from .evaluator import ExecutionLimits, TrustedEvaluator
from .taskpack import whitelist_example


def score_reward(
    definition: RewardDefinition,
    example: dict[str, Any],
    scoring_context: TaskPack,
    runner: Runner,
    *,
    limits: ExecutionLimits | None = None,
) -> ScoreResult:
    """Use the existing runner, whitelist and aggregator without synthesis.

    The caller owns the runner, its judge service, budgets and evidence storage.
    Each invocation has a distinct execution ID, including repeated rollouts of
    the same sample. Framework-specific result conversion belongs in adapters.
    """
    evaluator = TrustedEvaluator(runner, limits=limits)
    results, usage = evaluator.score_examples(
        definition,
        scoring_context,
        [whitelist_example(example, scoring_context.permitted_inputs)],
        ['candidate'],
        action_id='score:' + uuid4().hex,
        reward_key=reward_key_for(definition),
    )
    result = results[0]
    result.usage = usage
    return result
