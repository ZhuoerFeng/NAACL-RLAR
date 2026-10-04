"""Frozen inputs plus append-only call attempts; results are committed atomically."""
import hashlib
import json
import os
import random
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import RolloutConfig


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else dumps(data).encode()).hexdigest()


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    write_bytes(path, (dumps(value) + "\n").encode())


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as file:
        file.write(data)
        file.flush()
        os.fsync(file.fileno())
    os.replace(tmp, path)


def jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def code_digest():
    return digest({p.name: digest(p.read_bytes()) for p in sorted(Path(__file__).parent.glob("*.py"))})


def new_run(project, task):
    if not re.fullmatch(r"[A-Z][A-Za-z0-9]*", task):
        raise ValueError("Task name must be PascalCase")
    parent = Path(project).resolve() / "codebase/reward_harness/runs"
    parent.mkdir(parents=True, exist_ok=True)
    stem = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y_%m_%d_%H_%M_") + task
    for i in range(10000):
        path = parent / (stem + (f"Run{i + 1}" if i else ""))
        try:
            path.mkdir()
            return path
        except FileExistsError:
            continue
    raise ValueError("Unable to allocate a new run directory")


def prepare(project, cfg, task="QwenNineBRollout", limit=None):
    dataset = Path(cfg.dataset).resolve()
    source = dataset / "train_prompts.jsonl"
    data = source.read_bytes()
    release = read(dataset / "dataset_manifest.json")
    expected = release["files"]["train_prompts.jsonl"]["sha256"]
    if digest(data) != expected:
        raise ValueError("Published train_prompts.jsonl fingerprint mismatch")
    contract_path = dataset / "rollout_contract.json"
    contract_bytes = contract_path.read_bytes()
    if digest(contract_bytes) != release["files"]["rollout_contract.json"]["sha256"]:
        raise ValueError("Published rollout contract fingerprint mismatch")
    rows, ids = [], set()
    for line_number, line in enumerate(data.decode("utf-8").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        sid, prompt, source_name = item["sample_id"], item["prompt"], item["data_source"]
        if not isinstance(sid, str) or not sid or sid in ids:
            raise ValueError("Empty/duplicate sample_id")
        if not isinstance(source_name, str) or not source_name:
            raise ValueError("Empty data_source")
        if (not isinstance(prompt, list) or not prompt or prompt[-1].get("role") != "user"
                or any(set(m) != {"role", "content"} or m["role"] not in ("system", "user", "assistant")
                       or not isinstance(m["content"], str) for m in prompt)):
            raise ValueError(f"Invalid text-only prompt at line {line_number}")
        ids.add(sid)
        # Explicit allowlist: references and evaluation assets never enter a request.
        rows.append(dict(sample_id=sid, data_source=source_name, prompt=prompt,
                         source_line=line_number, prompt_sha256=digest(prompt)))
    if len(rows) != cfg.expected_rows:
        raise ValueError(f"Expected {cfg.expected_rows} prompts; found {len(rows)}")
    if limit is not None and not 1 <= limit <= len(rows):
        raise ValueError("--limit must be between 1 and the dataset row count")
    random.Random(cfg.order_seed).shuffle(rows)
    rows = rows[:limit] if limit else rows
    if cfg.max_requests < len(rows) * cfg.samples_per_prompt:
        raise ValueError("max_requests is smaller than the first-pass request count")
    run = new_run(project, task)
    snapshot = ("".join(dumps(r) + "\n" for r in rows)).encode()
    write_bytes(run / "inputs/prompts.jsonl", snapshot)
    write_bytes(run / "inputs/rollout_contract.json", contract_bytes)
    write_bytes(run / "inputs/dataset_manifest.json", (dataset / "dataset_manifest.json").read_bytes())
    write(run / "config.json", cfg.model_dump())
    manifest = {
        "schema_version": "rlar.rollout.run.v1", "kind": "policy_rollout", "run_id": uuid.uuid4().hex,
        "created_at": now(), "model": cfg.model, "model_revision": cfg.model_revision,
        "dataset": {"version": dataset.name, "path": str(dataset), "sha256": digest(data),
                    "total_prompts": cfg.expected_rows},
        "selected_prompts": len(rows), "planned_rollouts": len(rows) * cfg.samples_per_prompt,
        "selection": {"order_seed": cfg.order_seed, "limit": limit, "method": "seeded_shuffle_then_prefix"},
        "config_sha256": digest(cfg.model_dump()), "code_sha256": code_digest(),
        "inputs": {p.name: digest(p.read_bytes()) for p in (run / "inputs").iterdir()},
        "source_counts": dict(Counter(r["data_source"] for r in rows)),
        "contract": json.loads(contract_bytes),
        "effective_generation": cfg.generation_parameters(),
        "thinking_control": "chat_template_kwargs.enable_thinking; platform support requires real validation",
        "configured_transport": "httpx_chat_completions_stream",
    }
    write(run / "manifest.json", manifest)
    return run


def load_run(run, *, for_sampling=False):
    run = Path(run)
    manifest = read(run / "manifest.json")
    if manifest.get("schema_version") != "rlar.rollout.run.v1":
        raise ValueError("Not a supported policy rollout run")
    cfg = RolloutConfig.model_validate(read(run / "config.json"))
    if digest(cfg.model_dump()) != manifest["config_sha256"]:
        raise ValueError("Frozen configuration changed; prepare a new run")
    for name, expected in manifest["inputs"].items():
        if digest((run / "inputs" / name).read_bytes()) != expected:
            raise ValueError(f"Frozen input changed: {name}")
    if for_sampling:
        if code_digest() != manifest["code_sha256"]:
            raise ValueError("Rollout implementation changed; prepare a new run")
        if digest((Path(cfg.dataset) / "train_prompts.jsonl").read_bytes()) != manifest["dataset"]["sha256"]:
            raise ValueError("Source dataset changed; refusing resume")
    return manifest, cfg, jsonl(run / "inputs/prompts.jsonl")


def tasks(manifest, cfg, prompts):
    for prompt in prompts:
        for index in range(cfg.samples_per_prompt):
            rid = digest([manifest["run_id"], prompt["sample_id"], index])
            yield {**prompt, "rollout_id": rid, "sample_index": index}


def attempts(run, task):
    parent = Path(run) / "calls" / task["rollout_id"]
    return sorted(p for p in parent.iterdir() if p.is_dir()) if parent.exists() else []


def latest(run, task):
    paths = attempts(run, task)
    if not paths:
        return {"status": "pending"}
    path = paths[-1] / "result.json"
    # A reserved request with no committed result is ambiguous after a crash.
    return read(path) if path.exists() else {"status": "uncertain", "attempt": len(paths)}


def summarize(run):
    manifest, cfg, prompts = load_run(run)
    counts, tokens, source_counts, evidence_counts = Counter(), Counter(), {}, Counter()
    n_calls, unknown_usage = 0, 0
    for task in tasks(manifest, cfg, prompts):
        status = latest(run, task)["status"]
        counts[status] += 1
        source_counts.setdefault(task["data_source"], Counter())[status] += 1
        for path in attempts(run, task):
            n_calls += 1
            result = read(path / "result.json") if (path / "result.json").exists() else {}
            evidence_counts[result.get("evidence_kind", "uncommitted")] += 1
            usage = result.get("usage") or {}
            if not result.get("usage_complete"):
                unknown_usage += 1
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if isinstance(value, int) and value >= 0:
                    tokens[key] += value
    return {"run_id": manifest["run_id"], "model": cfg.model, "planned": manifest["planned_rollouts"],
            "status_counts": dict(counts), "source_counts": source_counts,
            "reserved_attempts": n_calls, "max_requests": cfg.max_requests,
            "attempts_by_evidence_kind": dict(evidence_counts),
            "observed_tokens_all_attempts": dict(tokens), "attempts_with_unknown_final_usage": unknown_usage,
            "cost": None, "cost_note": "No platform pricing configured; retries and incomplete usage remain explicit"}
