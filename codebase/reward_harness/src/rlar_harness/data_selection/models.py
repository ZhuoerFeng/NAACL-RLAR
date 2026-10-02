from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import ModelConfig

SOURCES = ("helpsteer2", "no_robots", "ultrafeedback", "kodcode", "taco", "mathqa")
TASKS = Literal["coding", "math_problem", "qa", "explanation", "advice", "writing",
                "rewrite", "summarization", "brainstorming", "other"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SelectionConfig(Strict):
    version: Literal[1] = 1
    seed: int = 20261002
    targets: dict[str, int]
    candidate_multiplier: int = Field(default=3, ge=1, le=10)
    max_requests: int = Field(default=4000, ge=1)
    concurrency: int = Field(default=6, ge=1, le=32)
    classifier_max_input_chars: int = Field(default=150000, ge=1000)
    near_duplicate_threshold: float = Field(default=0.9, ge=0.5, le=1.0)
    # Only UltraFeedback lacks an official split in this selection plan.
    ultrafeedback_holdout_fraction: float = Field(default=0.2, ge=0.0, lt=1.0)
    general_sources_only: bool = True
    classifier: ModelConfig
    policy_thinking: Literal[False] = False
    policy_max_new_tokens: Literal[8000] = 8000

    @model_validator(mode="after")
    def check(self):
        if not self.targets or set(self.targets) - set(SOURCES):
            raise ValueError("Targets may only use the six training sources")
        if any(type(n) is not int or n < 0 for n in self.targets.values()) or sum(self.targets.values()) < 1:
            raise ValueError("Targets must be nonnegative integer counts, with a positive total")
        if self.classifier.provider_adapter == "scripted":
            raise ValueError("Production selection requires a real HTTP adapter")
        u = urlsplit(self.classifier.endpoint or "")
        if u.scheme not in ("http", "https") or u.username or u.password or u.query or u.fragment:
            raise ValueError("Endpoint must be HTTP(S), without embedded credentials or query parameters")
        if self.classifier.model == "REQUIRED":
            raise ValueError("Classifier model must be explicit")
        return self


class Classification(Strict):
    decision: Literal["include", "exclude", "uncertain"]
    task_type: TASKS
    task_substance: Literal["nontrivial", "trivial", "unclear"]
    context_status: Literal["complete", "incomplete", "unclear"]
    evaluation_mode: Literal["verifiable", "llm_judged", "hybrid"]
    evaluation_dimensions: list[str] = Field(max_length=8)
    output_budget_risk: Literal["within_or_unknown", "explicitly_over_8000"]
    exclusion_codes: list[Literal["no_substantive_task", "missing_context", "unavailable_input",
                                  "unavailable_interaction", "explicit_output_over_budget",
                                  "uninterpretable_task"]] = Field(max_length=6)
    evidence: str = Field(min_length=1, max_length=1200)

    @model_validator(mode="after")
    def consistent(self):
        if any(not d.strip() for d in self.evaluation_dimensions):
            raise ValueError("Evaluation dimensions must be nonempty")
        if self.decision == "include":
            if self.exclusion_codes or not self.evaluation_dimensions:
                raise ValueError("Include requires dimensions and no exclusion codes")
            if self.context_status != "complete" or self.task_substance != "nontrivial":
                raise ValueError("Include requires a complete substantive task")
            if self.output_budget_risk == "explicitly_over_8000":
                raise ValueError("Include contradicts explicit output budget violation")
        if self.decision == "exclude" and not self.exclusion_codes:
            raise ValueError("Exclusion requires a concrete reason code")
        return self


def load_config(path: Path) -> SelectionConfig:
    return SelectionConfig.model_validate(yaml.safe_load(path.read_text()))
