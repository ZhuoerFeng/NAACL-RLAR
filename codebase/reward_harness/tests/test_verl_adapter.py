"""Offline adapter tests; subprocess execution is real, judge replies are fixtures."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import time

import pytest

from conftest import definition
from rlar_harness.adapters.verl import VerlRewardAdapter
from rlar_harness.evaluation.scoring import score_reward
from rlar_harness.evaluation.taskpack import TaskPackStore
from rlar_harness.runtime.runner import SubprocessRunner
from rlar_harness.schemas import RewardDefinition, ScoreResult

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def pack():
    return TaskPackStore(ROOT / 'tests/fixtures/math/task_packs').get('math_v2')


def extra(**metadata):
    return {'rlar': {'query': 'Compute 1/2 + 1/3', 'metadata': metadata}}


def synthetic_result(score=0.5, status='ok'):
    """Adapter boundary fixture only, not evidence of reward execution."""
    return ScoreResult(status=status, total_score=score, component_results=[],
        valid_component_ids=[], coverage=1.0, planned_count=0, successful_count=0,
        reward_key='adapter-fixture')


@pytest.mark.parametrize('response, expected', [(r'\boxed{5/6}', 1.0), (r'\boxed{1}', 0.0)])
def test_single_reward_matches_direct_harness_score(pack, response, expected):
    reward = definition(mode='single')
    runner = SubprocessRunner()
    adapter = VerlRewardAdapter.from_reward(reward, pack, runner)
    direct = score_reward(reward, {'query': extra()['rlar']['query'],
        'response': response, 'reference': '5/6'}, pack, runner)
    output = adapter.compute_score('math', response, '5/6', extra())
    assert output == {'score': direct.total_score} == {'score': expected}


def test_checklist_uses_frozen_normalization_once(pack):
    raw = definition(sources=['def score(e,c): return 2', 'def score(e,c): return 0']).model_dump()
    raw['components'][0]['normalization'] = {'kind': 'mapping', 'mapping_id': 'twoPoint'}
    reward = RewardDefinition.model_validate(raw)
    pack.normalization_mappings = {'twoPoint': {'kind': 'linear', 'source_range': [0, 2]}}
    adapter = VerlRewardAdapter.from_reward(reward, pack, SubprocessRunner())
    assert adapter.compute_score('math', 'answer', 'reference', extra()) == {'score': 0.5}


def test_mapping_preserves_raw_values_and_filters_framework_fields():
    original = extra(topic={'unit': 'clips'})
    original.update(expected_label='pass', rollout_reward_scores={'reward': 99}, tools_kwargs={'secret': 'hidden'})
    original['rlar'].update(query_id='sample-id', reward_key='artifact-id')
    reference = {'solution': 'Work shown. #### 72'}
    before = deepcopy((original, reference))
    seen = []

    def scorer(example):
        seen.append(deepcopy(example))
        example['metadata']['topic']['unit'] = 'changed'
        example['reference']['solution'] = 'changed'
        return synthetic_result()

    adapter = VerlRewardAdapter(scorer)
    assert adapter.compute_score('gsm8k', 'Work shown. #### 72', reference, original,
        reward_router_address='unused', reward_model_tokenizer=object()) == {'score': 0.5}
    assert (original, reference) == before
    assert seen == [{'query': original['rlar']['query'], 'response': 'Work shown. #### 72',
        'reference': before[1], 'metadata': {'topic': {'unit': 'clips'}}, 'topic': {'unit': 'clips'}}]


def test_task_pack_whitelist_is_still_applied(pack):
    source = "def score(e,c): return float('private' not in e and 'metadata' not in e)"
    adapter = VerlRewardAdapter.from_reward(definition(sources=[source]), pack, SubprocessRunner())
    assert adapter.compute_score('math', 'answer', 'reference', extra(private='hidden')) == {'score': 1.0}


def test_custom_dataset_mapper_can_use_data_source_and_extra_info():
    seen = []

    def mapper(data_source, solution_str, ground_truth, extra_info):
        return {'query': extra_info['question'], 'response': solution_str,
            'reference': ground_truth, 'metadata': {'dataset': data_source}}

    def scorer(example):
        seen.append(example)
        return synthetic_result()

    adapter = VerlRewardAdapter(scorer, input_mapper=mapper)
    adapter.compute_score('custom', 'answer', None, {'question': 'question'})
    assert seen == [{'query': 'question', 'response': 'answer', 'reference': None,
        'metadata': {'dataset': 'custom'}}]


@pytest.mark.parametrize('context', [None, {}, {'rlar': {}}, {'rlar': {'query': ''}},
    {'rlar': {'query': 'question', 'metadata': {'response': 'injected'}}}])
def test_bad_context_is_not_scored_or_returned_as_zero(context):
    def scorer(_):
        pytest.fail('invalid input must not reach the scorer')

    with pytest.raises(ValueError):
        VerlRewardAdapter(scorer).compute_score('math', 'answer', 'reference', context)


@pytest.mark.parametrize('sources', [
    ['def score(e,c): return 1/0'],
    ['def score(e,c): return 1', 'def score(e,c): return 1/0'],
])
def test_failed_and_partial_execution_have_no_framework_result(pack, sources):
    adapter = VerlRewardAdapter.from_reward(definition(sources=sources), pack, SubprocessRunner())
    with pytest.raises(ValueError, match='complete reward result'):
        adapter.compute_score('math', 'answer', 'reference', extra())


@pytest.mark.parametrize('score', [None, float('nan'), float('inf')])
def test_no_nonfinite_or_missing_reward_is_forwarded(score):
    adapter = VerlRewardAdapter(lambda _: synthetic_result(score))
    with pytest.raises(ValueError, match='finite numeric'):
        adapter.compute_score('math', 'answer', 'reference', extra())


def test_scorer_exception_propagates_unchanged():
    error = TimeoutError('test timeout')

    def scorer(_):
        raise error

    with pytest.raises(TimeoutError) as caught:
        VerlRewardAdapter(scorer).compute_score('math', 'answer', 'reference', extra())
    assert caught.value is error


def test_one_adapter_serializes_a_shared_scorer():
    active = False

    def scorer(example):
        nonlocal active
        assert not active
        active = True
        time.sleep(0.005)
        result = synthetic_result(float(example['response']))
        active = False
        return result

    adapter = VerlRewardAdapter(scorer)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda n: adapter.compute_score('math', str(n / 10), None, extra()), range(8)))
    assert results == [{'score': n / 10} for n in range(8)]


def test_rubric_uses_injected_runner_service_and_distinct_execution_ids():
    raw = json.loads((ROOT / 'examples/self_contained/reward_responses.json').read_text())
    reward = RewardDefinition.model_validate(raw['default'][0]['actions'][0]['arguments']['definition'])
    pack = TaskPackStore(ROOT / 'examples/self_contained/task_packs').get('gsm8k_self_contained')

    class FixtureJudgeService:
        def __init__(self):
            self.requests = []
            self.bindings = []

        def bind_execution(self, request, component):
            self.bindings.append(request.request_id)

        def handle_scoring_request(self, payload):
            self.requests.append(payload)
            return {'status': 'ok', 'raw_response': '{"score": 1, "feedback": "Authored fixture"}'}

    service = FixtureJudgeService()
    adapter = VerlRewardAdapter.from_reward(reward, pack, SubprocessRunner(scoring_service=service))
    for _ in range(2):
        assert adapter.compute_score('gsm8k', '#### 72', 'Work shown. #### 72', extra()) == {'score': 1.0}
    assert len(service.requests) == 2
    assert all(p['kind'] == 'call_llm_api' for p in service.requests)
    # The runner binds each component; the two separate scores must not reuse IDs.
    assert len(set(service.bindings)) == 2


def test_example_entrypoint_loads_frozen_files_and_uses_explicit_work_dir(pack, tmp_path):
    path = ROOT / 'examples/verl_reward.py'
    spec = importlib.util.spec_from_file_location('verl_reward_example', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    reward_path, pack_path = tmp_path / 'reward.json', tmp_path / 'task_pack.json'
    reward_path.write_text(definition(mode='single').model_dump_json())
    pack_path.write_text(pack.model_dump_json())
    kwargs = {'reward_path': str(reward_path), 'task_pack_path': str(pack_path),
        'execution_run_dir': str(tmp_path)}
    for _ in range(2):
        assert module.compute_score('math', r'\boxed{5/6}', '5/6', extra(), **kwargs) == {'score': 1.0}
    assert module._load_adapter.cache_info().misses == 1
    adapter = module._load_adapter(**kwargs)
    assert adapter._scorer.keywords['runner'].work_root == tmp_path / 'workers'
