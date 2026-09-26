"""Regenerate committed versioned JSON schemas from the authoritative models."""
import json
from pathlib import Path
from rlar_harness.schemas import QueryRecord, RewardDefinition, ScoreResult, ConstructionResult, TaskPack, ValidationReport, ToolResult
from rlar_harness.config import HarnessConfig, RunManifest
root=Path(__file__).resolve().parents[1]/'schemas'
root.mkdir(exist_ok=True)
for model in (QueryRecord,RewardDefinition,ScoreResult,ConstructionResult,TaskPack,ValidationReport,ToolResult,HarnessConfig,RunManifest):
    schema=model.model_json_schema()
    version=model.model_fields['schema_version'].default
    (root/(version+'.json')).write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n')
