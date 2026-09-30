from __future__ import annotations
from conftest import run_path
import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from rlar_harness.driver import construct_file, RunDriver
from rlar_harness.schemas import RewardDefinition, ComponentResult
from rlar_harness.storage.canonical import reward_key_for, digest
from rlar_harness.runtime.aggregate import aggregate
from rlar_harness.runtime.runner import SubprocessRunner
from rlar_harness.cli import score_reward
from rlar_harness.evaluation.taskpack import TaskPackStore
from rlar_harness.evaluation.evaluator import TrustedEvaluator
from rlar_harness.llm.scripted import ScriptedLLM
from rlar_harness.inputs import iter_jsonl
from conftest import definition, turn


def evaluator(config):
    pack = TaskPackStore(Path(config.data.task_pack_root)).get('math_v2')
    suite = None
    return TrustedEvaluator(SubprocessRunner(), integrity_secret=b'fixture'), pack, suite


def validate(config, d, tmp_path):
    from rlar_harness.trace.export import read_run
    cfg=config.model_copy(deep=True);cfg.synthesis.reward_synthesis_attempts=1
    root=run_path(tmp_path, 'Validation')
    construct_file(cfg,cfg.data.input_path,root,llm_client=ScriptedLLM([turn(d)]))
    blobs,events,_,_=read_run(root)
    from rlar_harness.schemas import ValidationReport
    return ValidationReport.model_validate(blobs.get_json(next(e.payload['validation_ref'] for e in events if e.type=='evaluation_result')))

@pytest.mark.acceptance('AT-01')
def test_stream_errors_are_position_bound_and_read_only(config, source, tmp_path):
    original = source.read_bytes()
    source.write_bytes(original + b'{invalid\n' + b'{"query_id":"missing"}\n' + original + original.replace(b'q1', b'q2'))
    before = source.read_bytes()
    rows = construct_file(config, source, run_path(tmp_path, 'TestRun'))
    assert [r.status for r in rows] == ['success', 'failed', 'failed', 'failed', 'success']
    assert rows[3].stop_reason == 'duplicate_query_id'
    assert len({r.job_key for r in rows}) == 5
    assert [r.input_location.line for r in rows] == [1, 2, 3, 4, 5]
    assert before == source.read_bytes()

@pytest.mark.acceptance('AT-02')
def test_layouts_and_rebuilt_index(config, source, tmp_path):
    keys = []
    for layout in ('inline', 'reference'):
        c = config.model_copy(deep=True); c.data.output_layout = layout
        root = run_path(tmp_path, layout.title())
        r = construct_file(c, source, root, llm_client=ScriptedLLM([turn()]))[0]
        keys.append(r.reward_key)
        with RunDriver(root, resume=True) as driver:
            assert driver.library.get(r.reward_key).definition.components[0].source.startswith('def score')
            assert list(driver.process(iter_jsonl(source))) == []
        assert (r.reward_definition is not None) == (layout == 'inline')
    assert keys[0] == keys[1]

@pytest.mark.acceptance('AT-03')
def test_hash_golden_and_full_contract_binding(config, tmp_path):
    d = definition('0.0', api=False)
    golden = {'key': reward_key_for(d)}
    assert reward_key_for(d) == golden['key']
    data = d.model_dump(mode='json')
    data['components'][0]['source'] = data['components'][0]['source'].replace('\n', '\r\n')
    assert reward_key_for(data) == reward_key_for(d)
    for change in ('source', 'criterion', 'normalization', 'runtime', 'order'):
        data = d.model_dump(mode='json')
        if change == 'source': data['components'][0]['source'] += '# new\n'
        elif change == 'criterion': data['components'][0]['criterion'] += ' different'
        elif change == 'normalization': data['components'][0]['normalization'] = {'kind': 'mapping', 'mapping_id': 'frozen'}
        elif change == 'runtime': data['runtime_contract']['dependencies'] = ['pydantic==2.13.5']
        else: data['components'].append({**data['components'][0], 'id': 'other', 'source':data['components'][0]['source']+'# distinct\n'})
        assert reward_key_for(data) != reward_key_for(d)
    report = validate(config, d, tmp_path)
    assert reward_key_for(d) == golden['key']
    assert report.reward_key == golden['key']

@pytest.mark.acceptance('AT-04', 'AT-05', 'AT-06')
@pytest.mark.parametrize('expressions,expected,status', [(['1','0','0.5'], .5, 'ok'),
    (['1','1/0','0'], .5, 'partial'), (['1/0','1/0'], None, 'failed'),
    (['0','0'], 0., 'ok'), (["float('nan')", '1'], 1., 'partial'),
    (["float('inf')", '0'], 0., 'partial'), (['True', '0'], 0., 'partial')])
def test_real_component_execution(config, expressions, expected, status):
    _, pack, _ = evaluator(config)
    d = definition(sources=['def score(example, context):\n    return '+e+'\n' for e in expressions], api=False)
    result = score_reward(d, {'query':'x','response':'y','reference':'0'}, pack, SubprocessRunner())
    assert result.status == status
    assert result.total_score == expected
    assert result.coverage == result.successful_count / len(expressions)
    assert [c.id for c in result.component_results] == d.component_ids

@pytest.mark.acceptance('AT-06')
def test_component_load_failures_old_abi_rejection_and_state_reset(config):
    _, pack, _ = evaluator(config)
    d = definition(sources=['this is invalid python !', 'import definitely_missing_module', 'def score(e,c): return True'], api=False)
    r = score_reward(d, {}, pack, SubprocessRunner())
    assert r.status == 'failed'
    changed = d.model_dump(mode='json'); changed['runtime_contract']['scoring_abi'] = 'v1+bool'
    with pytest.raises(ValidationError): RewardDefinition.model_validate(changed)
    counter = definition(sources=['count=0\ndef score(e,c):\n global count\n count+=1\n return count\n'], api=False)
    ev, pack, suite = evaluator(config)
    rs, _ = ev.score_examples(counter, pack, [{}, {}], ['a','b'], action_id='reset', reward_key=reward_key_for(counter))
    assert [r.total_score for r in rs] == [1, 1]

@pytest.mark.acceptance('AT-07')
@pytest.mark.parametrize('variant', ['weights', 'empty', 'duplicate', 'weighted', 'single_mean'])
def test_reject_unsupported_aggregation(variant):
    d = definition().model_dump(mode='json')
    if variant == 'weights': d['weights'] = [1]
    elif variant == 'empty': d['components'] = []
    elif variant == 'duplicate': d['components'] *= 2
    elif variant == 'weighted': d['aggregation']['kind'] = 'weighted_mean'
    else: d['mode'] = 'single'
    with pytest.raises(ValidationError): RewardDefinition.model_validate(d)

@pytest.mark.acceptance('AT-08', 'AT-03')
def test_forged_and_stale_reports_cannot_finalize(config, source, tmp_path):
    with RunDriver(run_path(tmp_path, 'TestRun'), config=config, input_path=source) as driver:
        item = next(iter_jsonl(source)); pack = driver.packs.get('math_v2')
        from conftest import start_episode
        state, budget, dispatch, _ = start_episode(driver,item,config)
        from rlar_harness.tools.dispatcher import TestArgs
        r = dispatch.test_reward(TestArgs(definition=definition(), dev_profile_id='math_v2'), 'test')
        dispatch.finalize(r['definition_ref'], r['validation_ref'])
        raw = driver.blobs.get_json(r['validation_ref']); raw['eligible'] = False
        with pytest.raises(ValueError): dispatch.finalize(r['definition_ref'], driver.blobs.put_json(raw))
        raw['eligible'] = True; raw['metrics']['false_accept_rate'] = .25
        with pytest.raises(ValueError): dispatch.finalize(r['definition_ref'], driver.blobs.put_json(raw))
        dref = driver.blobs.put_json(definition('1.0').model_dump(mode='json'))
        with pytest.raises(ValueError, match='mismatch'): dispatch.finalize(dref, r['validation_ref'])
        with pytest.raises((ValueError, ValidationError)): dispatch.finalize(r['definition_ref'], driver.blobs.put_json({'PASS': True}))
        dispatch.suite = dispatch.suite.model_copy(update={'version':'changed'})
        with pytest.raises(ValueError, match='mismatch'): dispatch.finalize(r['definition_ref'], r['validation_ref'])

@pytest.mark.acceptance('AT-09')
def test_running_reward_is_not_quality_pass(config, tmp_path):
    r = validate(config, definition('1.0'), tmp_path)
    assert r.metrics.completed_cases == r.metrics.planned_cases
    assert any(d.passed is False for d in r.decisions)
    assert not r.eligible

@pytest.mark.acceptance('AT-10')
def test_partial_missing_denominators_and_unvalidated(config, source, tmp_path):
    config.synthesis.test_synthesis_attempts=1
    from rlar_harness.llm.scripted import Turn
    rows=construct_file(config,source,run_path(tmp_path, 'MissingSuite'),llm_client={'test_case_synthesizer':ScriptedLLM([Turn('{}')])})
    assert rows[0].status=='failed' and rows[0].stop_reason=='test_suite_unavailable'
    from conftest import reward_calls
    assert reward_calls(run_path(tmp_path, 'MissingSuite'))==[]

@pytest.mark.acceptance('AT-01','AT-03')
def test_preflight_validates_profiles_and_reports_all_placeholders(config,tmp_path):
    from rlar_harness.config import preflight, load_config
    from rlar_harness.errors import PreflightError
    from conftest import ROOT
    with pytest.raises(PreflightError) as exc:
        load_config(ROOT/'configs/real_service.template.yaml')
    assert any('endpoint' in p for p in exc.value.problems)
    packpath=tmp_path/'packs';packpath.mkdir()
    raw=json.loads((Path(config.data.task_pack_root)/'math_v2.json').read_text())
    raw['suite_policy']['objective_checker']='forbidden';(packpath/'math_v2.json').write_text(json.dumps(raw))
    config.data.task_pack_root=str(packpath)
    result=preflight(config,ROOT,runner_capabilities=SubprocessRunner().capabilities().model_dump())
    assert not result.ok and any('invalid task pack' in p for p in result.problems)
