from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class RolloutConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)

    schema_version: Literal["rlar.rollout.config.v1"] = "rlar.rollout.config.v1"
    dataset: str
    expected_rows: int = Field(default=1000, ge=1)
    model: str = Field(min_length=1)
    model_revision: str | None = None
    endpoint: str
    api_key_env: str = Field(default="TMP_TOKEN", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    temperature: float = Field(default=0.7, ge=0, le=2)
    top_p: float = Field(default=0.6, gt=0, le=1)
    top_k: int = Field(default=20, ge=1)
    beam_size: Literal[1] = 1
    repetition_penalty: float = Field(default=1.05, gt=0)
    max_tokens: int = Field(default=8000, ge=1)
    thinking: Literal["disabled", "enabled", "server_default"] = "disabled"
    samples_per_prompt: int = Field(default=1, ge=1)
    # Local ordering seed; do not imply that the platform implements model seeds.
    order_seed: int = 20261004
    generation_seed: int | None = None
    concurrency: int = Field(default=4, ge=1, le=64)
    requests_per_minute: int = Field(default=60, ge=1)
    request_timeout_seconds: float = Field(default=600, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=20)
    max_requests: int = Field(default=3000, ge=1)
    retry_backoff_seconds: float = Field(default=2, ge=0)

    @model_validator(mode="after")
    def validate_endpoint(self):
        url = urlsplit(self.endpoint)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("Use an HTTP(S) endpoint without credentials/query/fragment")
        return self

    def generation_parameters(self, sample_index=0):
        body = {key: getattr(self, key) for key in (
            "temperature", "top_p", "top_k", "beam_size", "repetition_penalty", "max_tokens")}
        body.update(model=self.model, stream=True,
                    stream_options={"include_usage": True, "continuous_usage_stats": True})
        if self.thinking != "server_default":
            body["chat_template_kwargs"] = {"enable_thinking": self.thinking == "enabled"}
        if self.generation_seed is not None:
            body["seed"] = self.generation_seed + sample_index
        return body


def load_config(path):
    path = Path(path).resolve()
    cfg = RolloutConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    cfg.dataset = str((path.parent / cfg.dataset).resolve())
    return cfg
