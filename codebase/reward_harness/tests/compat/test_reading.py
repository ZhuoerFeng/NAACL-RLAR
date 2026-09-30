"""Frozen historical bytes stay readable after retiring historical execution."""
from conftest import run_path
import json
from pathlib import Path

import pytest

from rlar_harness.compat.reading import StoredRecord
from rlar_harness.config import require_current_manifest
from rlar_harness.errors import ConfigError
from rlar_harness.storage.canonical import digest
from rlar_harness.trace.export import read_run, replay, export_llm_calls, export_sft
from rlar_harness.trace.report import report

FIXTURES = Path(__file__).parent / 'fixtures'


@pytest.mark.acceptance('UAT-28', 'SC-05', 'AT-08')
def test_historical_report_signature_verifies_without_new_defaults():
    from rlar_harness.evaluation.evaluator import TrustedEvaluator
    data = json.loads((FIXTURES / 'historical_signature.json').read_text())
    evaluator = TrustedEvaluator(None, integrity_secret=data['test_key'].encode())
    assert evaluator.verify(StoredRecord(data['report']))
    changed = dict(data['report'], eligible=not data['report']['eligible'])
    assert not evaluator.verify(StoredRecord(changed))


@pytest.mark.acceptance('UAT-28', 'SC-05', 'AT-03')
def test_historical_serialized_contracts_keep_hash_and_defaults():
    data=json.loads((FIXTURES/'serialized_contracts.json').read_text())
    reward=StoredRecord(data['reward'])
    assert digest(reward.model_dump())==data['reward_key']
    assert digest(data['config'])==data['config_digest']
    assert 'capabilities' not in reward.model_dump()
    assert 'reward_logic_policy' not in reward.runtime_contract.model_dump()
    with pytest.raises(ConfigError,match='historical execution'):
        require_current_manifest({'config':data['config']})


@pytest.mark.acceptance('UAT-28', 'SC-05', 'AT-30', 'AT-35', 'AT-36')
def test_historical_trace_report_replay_and_explicit_training_export(tmp_path):
    bundle=json.loads((FIXTURES/'legacy_trace.json').read_text())
    root=run_path(tmp_path, 'HistoricalRead')
    for name,content in bundle['files'].items():
        p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
    before={p:p.read_bytes() for p in root.rglob('*') if p.is_file()}
    assert replay(root)==bundle['expected_replay']
    expected=bundle['expected_report']
    actual=report(root)
    for key in ('counts','episodes','physical_requests','actor_usage','validation_metrics'):
        assert actual[key]==expected[key]
    export_llm_calls(root,root/'exports/calls.jsonl')
    assert (root/'exports/calls.jsonl').read_text()==bundle['expected_calls']
    assert export_sft(root,root/'exports/default.jsonl')['samples']==0
    for fmt in ('per_call','full_trace','final_program_only'):
        result=export_sft(root,root/'exports'/f'{fmt}.jsonl',include_legacy=True,format=fmt)
        assert result['samples']==1 and result['schema_version']=='rlar.export.v1'
        row=json.loads((root/'exports'/f'{fmt}.jsonl').read_text())
        assert row['schema_version']=='rlar.sft.v1'
    assert export_sft(root,root/'exports/tests.jsonl',actor_role='test_case_synthesizer',include_legacy=True)['samples']==0
    assert all(p.read_bytes()==value for p,value in before.items())
    with pytest.raises(ConfigError):require_current_manifest(read_run(root)[3])
