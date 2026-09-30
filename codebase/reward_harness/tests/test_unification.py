"""Version consolidation boundaries, independent of authored model answers."""
import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from rlar_harness.config import HarnessConfig, load_config, RunManifest
from rlar_harness.schemas import RewardDefinition, TaskPack, ConstructionResult
from rlar_harness.storage.canonical import digest

ROOT=Path(__file__).resolve().parents[1]


def test_config_alias_is_one_canonical_wire_form(config):
    wire=config.model_dump(mode='json')
    assert 'v2' in wire and 'synthesis' not in wire
    current={**wire,'synthesis':wire['v2']};del current['v2']
    parsed=HarnessConfig.model_validate(current)
    assert parsed.model_dump(mode='json')==wire
    assert parsed.digest()==config.digest()
    assert HarnessConfig.model_validate_json(parsed.model_dump_json()).digest()==config.digest()
    with pytest.raises(ValidationError,match='exactly one'):
        HarnessConfig.model_validate({**wire,'synthesis':wire['v2']})


def test_current_schemas_bind_nested_contracts():
    schema=RewardDefinition.model_json_schema()
    contract=schema['$defs']['RuntimeContract']['properties']
    assert contract['scoring_abi']['const']==contract['scoring_abi']['default']=='v2'
    assert contract['reward_logic_policy']['const']=='self_contained_v1'
    assert {'kind','capability_ids'} <= set(schema['$defs']['Component']['required'])
    assert schema['properties']['schema_version']['const']=='rlar.reward.v2'
    assert TaskPack.model_json_schema()['properties']['schema_version']['const']=='rlar.taskpack.v2'
    for model in (HarnessConfig,RunManifest,ConstructionResult):
        assert model.model_json_schema()['properties']['schema_version']['const'].endswith('.v2')
    properties=HarnessConfig.model_json_schema()['properties']
    assert 'v2' in properties and not {'synthesis','model','rm'} & properties.keys()


@pytest.mark.parametrize('change',['config','policy','abi','reward'])
def test_old_executable_contracts_are_rejected(config,change):
    from conftest import definition
    if change in ('config','policy'):
        data=config.model_dump(mode='json')
        if change=='config':data['schema_version']='rlar.config.v1'
        else:data['v2']['reward_logic_policy']='legacy_checkers_v1'
        with pytest.raises(ValidationError):HarnessConfig.model_validate(data)
    else:
        data=definition().model_dump(mode='json')
        if change=='abi':data['runtime_contract']['scoring_abi']='v1'
        else:data['schema_version']='rlar.reward.v1'
        with pytest.raises(ValidationError):RewardDefinition.model_validate(data)


def test_current_source_has_no_legacy_execution_dependency():
    forbidden={'V2Config','is_v2','validate_v2','_export_sft_v2'}
    for path in (ROOT/'src/rlar_harness').rglob('*.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.ClassDef)):
                assert node.name not in forbidden,(path,node.name)
            if isinstance(node,ast.ImportFrom):
                assert 'legacy_checkers' not in (node.module or '')
                assert not (node.module or '').endswith('runtime.broker')
    assert not (ROOT/'src/rlar_harness/runtime/broker.py').exists()
    assert not (ROOT/'src/rlar_harness/compat/legacy_checkers.py').exists()


def test_fixture_generator_is_self_contained_and_does_not_edit_real_config(tmp_path):
    path=ROOT/'configs/aihub.yaml';before=path.read_bytes()
    output=tmp_path/'fixtures'
    result=subprocess.run([sys.executable,str(ROOT/'examples/make_fixtures.py'),'--output-dir',str(output)],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    cfg=load_config(output/'offline.yaml')
    assert cfg.synthesis.reward_logic_policy=='self_contained_v1'
    assert RewardDefinition.model_validate(json.loads((output/'reward_responses.json').read_text())['default'][0]['actions'][0]['arguments']['definition'])
    assert path.read_bytes()==before
