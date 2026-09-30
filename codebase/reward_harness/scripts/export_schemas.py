"""Emit v2 schemas without replacing frozen historical v1 schema files."""
import json
from pathlib import Path
from rlar_harness.schemas import (RewardDefinition, ScoreResult, ConstructionResult, TaskPack,
                                  ValidationReport, SuiteDraft, FrozenSuite, ValidationDecision)
from rlar_harness.config import HarnessConfig, RunManifest
root = Path(__file__).resolve().parents[1] / 'schemas'
models = {'rlar.reward.v2': RewardDefinition, 'rlar.score.v2': ScoreResult,
    'rlar.result.v2': ConstructionResult, 'rlar.taskpack.v2': TaskPack,
    'rlar.validation.v2': ValidationReport, 'rlar.config.v2': HarnessConfig,
    'rlar.manifest.v2': RunManifest, 'rlar.suite_draft.v2': SuiteDraft,
    'rlar.suite.v2': FrozenSuite, 'rlar.validation_decision.v2': ValidationDecision}
for version, model in models.items():
    schema = model.model_json_schema()
    if 'schema_version' in schema['properties']:
        assert schema['properties']['schema_version']['const'] == version
    schema['$id'] = version
    (root / (version + '.json')).write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n')
