"""Load one frozen verifiable reward through verl.custom_reward_function.

This is application scaffolding, not generated reward code. Files are read
once per worker and must remain immutable. For rubric rewards, build the same
VerlRewardAdapter with your configured durable scorer or judge-enabled runner.
"""
from functools import lru_cache
from pathlib import Path
from threading import Lock

from rlar_harness.adapters.verl import VerlRewardAdapter
from rlar_harness.runtime.runner import SubprocessRunner
from rlar_harness.schemas import RewardDefinition, TaskPack

_load_lock = Lock()


@lru_cache(maxsize=32)
def _load_adapter(reward_path, task_pack_path, execution_run_dir):
    definition = RewardDefinition.model_validate_json(Path(reward_path).read_text(encoding='utf-8'))
    pack = TaskPack.model_validate_json(Path(task_pack_path).read_text(encoding='utf-8'))
    if any(component.kind == 'rubric' for component in definition.components):
        raise ValueError('rubric rewards require a caller-configured judge service; use VerlRewardAdapter.from_reward')
    if definition.capabilities != pack.capabilities:
        raise ValueError('reward capabilities do not match the selected task pack')
    if definition.runtime_contract.environment_ref != pack.applicability_rule.runtime_fingerprint:
        raise ValueError('reward runtime contract does not match the selected task pack')
    if definition.mode not in pack.mode_constraints:
        raise ValueError('reward mode is not permitted by the selected task pack')
    runner = SubprocessRunner()
    runner.work_root = Path(execution_run_dir) / 'workers'
    runner.work_root.mkdir(parents=True, exist_ok=True)
    return VerlRewardAdapter.from_reward(definition, pack, runner)


def compute_score(
    data_source,
    solution_str,
    ground_truth,
    extra_info=None,
    *,
    reward_path,
    task_pack_path,
    execution_run_dir,
    **runtime_kwargs,
):
    # lru_cache alone permits duplicate initialization on concurrent cache misses.
    with _load_lock:
        adapter = _load_adapter(
            str(Path(reward_path).resolve()),
            str(Path(task_pack_path).resolve()),
            str(Path(execution_run_dir).resolve()),
        )
    return adapter.compute_score(data_source, solution_str, ground_truth, extra_info, **runtime_kwargs)
