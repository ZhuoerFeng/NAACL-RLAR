"""Offline engineering tests only: HTTP fixtures do not certify real platform behavior."""
import asyncio
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from rlar_harness.rollout.config import RolloutConfig
from rlar_harness.rollout.export import export_runs
from rlar_harness.rollout.sampler import replay, sample
from rlar_harness.rollout.store import (attempts, digest, jsonl, latest, load_run, prepare, read,
                                      summarize, tasks, write)
from rlar_harness.rollout.stream import parse_sse


TOKEN = "offline-fixture-credential"


@pytest.fixture
def project(tmp_path, monkeypatch):
    dataset = tmp_path / "codebase/data/datasets/fixture_v1"
    dataset.mkdir(parents=True)
    rows = [{"sample_id": f"s{i}", "data_source": "fixture", "prompt": [
        {"role": "user", "content": f"Question {i}"},
        {"role": "assistant", "content": "Prior assistant context"},
        {"role": "user", "content": "Continue in Chinese"}]} for i in range(3)]
    (dataset / "train_prompts.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    write(dataset / "rollout_contract.json", {"thinking": False, "max_new_tokens": 8000})
    write(dataset / "evaluation_assets.jsonl", {"reference": "SECRET_GOLD_MUST_NOT_LEAK"})
    write(dataset / "dataset_manifest.json", {"files": {name: {"sha256": digest((dataset / name).read_bytes())}
          for name in ("train_prompts.jsonl", "rollout_contract.json")}})
    monkeypatch.setenv("ROLLOUT_TEST_KEY", TOKEN)
    return tmp_path


def config(project, **updates):
    return RolloutConfig(dataset=str(project / "codebase/data/datasets/fixture_v1"), expected_rows=3,
                         endpoint="https://fixture.invalid/chat/completions", model=updates.pop("model", "fixture-model-A"),
                         api_key_env="ROLLOUT_TEST_KEY", requests_per_minute=1000000,
                         retry_backoff_seconds=0, concurrency=updates.pop("concurrency", 1), **updates)


def stream(content="你好", *, finish="stop", reasoning="", model="fixture-model-A", done=True):
    events = [
        {"model": model, "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning_content": reasoning}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11}},
        {"model": model, "choices": [{"index": 0, "delta": {"content": content}}],
         "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
        {"model": model, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
        {"model": model, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}},
    ]
    raw = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n" for event in events)
    return (raw + ("data: [DONE]\r\n\r\n" if done else "")).encode()


class Pieces(httpx.AsyncByteStream):
    def __init__(self, data, fail=False):
        self.data, self.fail = data, fail

    async def __aiter__(self):
        for i in range(0, len(self.data), 7):
            yield self.data[i:i + 7]
        if self.fail:
            raise httpx.ReadError("offline disconnection")


def execute(run, handler=None, **kwargs):
    handler = handler or (lambda request: httpx.Response(200, stream=Pieces(stream())))
    return asyncio.run(sample(run, transport=httpx.MockTransport(handler), **kwargs))


def test_prepare_freezes_inputs_and_never_overwrites(project):
    cfg = config(project)
    first, second = prepare(project, cfg), prepare(project, cfg)
    assert first != second
    manifest, _, prompts = load_run(first)
    assert manifest["planned_rollouts"] == 3
    assert jsonl(second / "inputs/prompts.jsonl") == prompts
    assert {r["source_line"] for r in prompts} == {1, 2, 3}
    assert "SECRET_GOLD" not in (first / "inputs/prompts.jsonl").read_text()
    write(first / "config.json", cfg.model_copy(update={"max_tokens": 1024}).model_dump())
    with pytest.raises(ValueError, match="configuration changed"):
        load_run(first)


def test_multiple_samples_resume_and_cumulative_usage(project):
    cfg = config(project, samples_per_prompt=2, generation_seed=42, concurrency=2)
    run = prepare(project, cfg)
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert body["max_tokens"] == 8000 and body["chat_template_kwargs"] == {"enable_thinking": False}
        assert len(body["messages"]) == 3 and "SECRET_GOLD" not in request.content.decode()
        return httpx.Response(200, stream=Pieces(stream()))
    assert execute(run, handler, max_new_calls=2)["new_calls"] == 2
    assert execute(run, handler)["new_calls"] == 4
    assert execute(run, handler)["new_calls"] == 0
    assert len({r["request_id"] for r in requests}) == 6
    assert {r["seed"] for r in requests} == {42, 43}
    assert summarize(run)["observed_tokens_all_attempts"] == {"prompt_tokens": 60, "completion_tokens": 18, "total_tokens": 78}
    assert replay(run)["checked_attempts"] == 6
    assert replay(run)["passed"]
    assert summarize(run)["attempts_by_evidence_kind"] == {"offline_fixture": 6}


def test_retry_has_distinct_attempts_and_persistent_budget(project):
    cfg = config(project, max_requests=3)
    run = prepare(project, cfg, limit=1)
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "busy"}) if len(seen) == 1 else httpx.Response(200, content=stream())
    execute(run, handler, max_new_calls=1)
    assert summarize(run)["status_counts"] == {"retryable": 1}
    execute(run, handler)
    assert len(seen) == 2 and seen[0]["request_id"] != seen[1]["request_id"]
    assert seen[0]["messages"] == seen[1]["messages"]
    assert summarize(run)["reserved_attempts"] == 2
    assert summarize(run)["attempts_with_unknown_final_usage"] == 1
    assert replay(run)["passed"]


def test_thirty_two_streams_overlap_without_exceeding_limit(project):
    cfg = config(project, concurrency=32, samples_per_prompt=12)
    cfg.requests_per_minute = 1920
    run = prepare(project, cfg)
    active, peak, started = 0, 0, 0

    async def exercise():
        reached_capacity = asyncio.Event()

        class HeldStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal active, peak, started
                active += 1
                started += 1
                peak = max(peak, active)
                if active == 32:
                    reached_capacity.set()
                try:
                    # A serial implementation cannot pass this barrier. Count the
                    # complete streaming lifetime, not just HTTP header delivery.
                    await asyncio.wait_for(reached_capacity.wait(), timeout=10)
                    await asyncio.sleep(0.02)
                    yield stream()
                finally:
                    active -= 1

        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=HeldStream()))
        return await sample(run, transport=transport)

    result = asyncio.run(exercise())
    assert result["status_counts"] == {"success": 36}
    assert started == 36 and peak == 32 and active == 0
    assert result["attempts_by_evidence_kind"] == {"offline_fixture": 36}
    assert replay(run)["passed"]
    write(project / "observed_concurrency.json", {"scope": "offline fixture, not platform load test",
          "configured_concurrency": 32, "requests_per_minute": 1920,
          "observed_peak_in_flight": peak, "completed": started})


def test_uncertain_stream_requires_explicit_retry(project):
    run = prepare(project, config(project), limit=1)
    execute(run, lambda r: httpx.Response(200, stream=Pieces(stream(done=False), fail=True)))
    assert summarize(run)["status_counts"] == {"uncertain": 1}
    assert execute(run)["new_calls"] == 0
    assert replay(run)["passed"]
    assert execute(run, retry_uncertain=True)["status_counts"] == {"success": 1}
    assert len(list(run.glob("calls/*/*/response.sse"))) == 2


def test_crash_reservation_preserved_and_retry_uses_next_attempt(project):
    run = prepare(project, config(project), limit=1)
    m, cfg, rows = load_run(run)
    task = next(tasks(m, cfg, rows))
    (run / "calls" / task["rollout_id"] / "0001").mkdir(parents=True)
    assert execute(run)["new_calls"] == 0
    assert len(replay(run)["incomplete_attempts"]) == 1
    execute(run, retry_uncertain=True)
    assert attempts(run, task)[-1].name == "0002"
    assert latest(run, task)["status"] == "success"


@pytest.mark.parametrize("raw,status", [
    (stream(reasoning="Private reasoning"), "thinking_violation"),
    (stream(model="wrong-model"), "model_mismatch"),
    (b"data: {broken}\n\ndata: [DONE]\n\n", "protocol_error"),
], ids=["thinking", "wrong-model", "malformed-sse"])
def test_contract_failure_stops_batch_and_plain_resume(project, raw, status):
    run = prepare(project, config(project))
    result = execute(run, lambda r: httpx.Response(200, content=raw))
    assert result["new_calls"] == 1
    assert result["status_counts"] == {status: 1, "pending": 2}
    assert execute(run)["new_calls"] == 0
    assert replay(run)["passed"]


def test_http_auth_failure_no_blind_retries(project):
    run = prepare(project, config(project))
    result = execute(run, lambda r: httpx.Response(401, json={"error": "invalid token"}))
    assert result["new_calls"] == 1
    assert execute(run)["new_calls"] == 0
    assert execute(run, retry_failed=True)["status_counts"] == {"success": 3}


def test_request_budget_survives_resume(project):
    run = prepare(project, config(project, max_requests=3))
    execute(run, lambda r: httpx.Response(503, json={"error": "offline unavailable"}))
    assert summarize(run)["reserved_attempts"] == 3
    result = execute(run)
    assert result["new_calls"] == 0 and result["stop_reason"] == "request_budget_exhausted"


def test_truncation_and_reasoning_remain_explicit(project):
    run = prepare(project, config(project, thinking="enabled"), limit=1)
    execute(run, lambda r: httpx.Response(200, content=stream(finish="length", reasoning="reasoning text")))
    output = Path(export_runs([run])["output"])
    assert jsonl(output / "candidates.jsonl") == []
    all_rows = jsonl(output / "rollouts.jsonl")
    assert all_rows[0]["status"] == "truncated" and all_rows[0]["reasoning_content"] == "reasoning text"
    assert jsonl(output / "by_prompt.jsonl")[0]["rollouts"] == []
    assert export_runs([run], include_truncated=True)["candidates"] == 1


def test_cross_chunk_credential_echo_never_written(project):
    run = prepare(project, config(project), limit=1)
    execute(run, lambda r: httpx.Response(200, stream=Pieces(stream(content="Echo " + TOKEN)),
                                         headers={"x-debug": TOKEN, "set-cookie": TOKEN}))
    for path in run.rglob("*"):
        if path.is_file():
            assert TOKEN.encode() not in path.read_bytes(), path
    result = read(next(run.glob("calls/*/*/result.json")))
    assert result["credential_echo_redacted"]
    assert result["content"] == "Echo [REDACTED]"
    assert replay(run)["passed"]


def test_merge_preserves_models_prompt_alignment_and_bundle(project):
    first = prepare(project, config(project))
    second = prepare(project, config(project, model="fixture-model-B"))
    execute(first)
    execute(second, lambda r: httpx.Response(200, content=stream(model="fixture-model-B")))
    result = export_runs([first, second, first], project=project, merge=True, bundle=True)
    assert result["candidates"] == 6 and result["prompts"] == 3
    output = Path(result["output"])
    grouped = jsonl(output / "by_prompt.jsonl")
    assert all({r["model"] for r in g["rollouts"]} == {"fixture-model-A", "fixture-model-B"} for g in grouped)
    rows = jsonl(output / "rollouts.jsonl")
    assert len({r["rollout_id"] for r in rows}) == 6
    assert all(r["dataset_sha256"] and r["response_sha256"] and r["source_line"] for r in rows)
    with tarfile.open(output / "evidence.tar.gz") as archive:
        names = archive.getnames()
        assert any(name.endswith("response.sse") for name in names)
        assert "export/by_prompt.jsonl" in names
        assert not any("run.lock" in name for name in names)


def test_merge_rejects_dataset_drift(project):
    first = prepare(project, config(project))
    dataset = project / "codebase/data/datasets/fixture_v1"
    path = dataset / "train_prompts.jsonl"
    path.write_text(path.read_text().replace("Question", "Changed question"))
    manifest = read(dataset / "dataset_manifest.json")
    manifest["files"]["train_prompts.jsonl"]["sha256"] = digest(path.read_bytes())
    write(dataset / "dataset_manifest.json", manifest)
    second = prepare(project, config(project))
    with pytest.raises(ValueError, match="different dataset"):
        export_runs([first, second], project=project, merge=True)
    with pytest.raises(ValueError, match="Source dataset changed"):
        execute(first)


def test_evidence_tamper_blocks_export(project):
    run = prepare(project, config(project), limit=1)
    execute(run)
    response = next(run.glob("calls/*/*/response.sse"))
    response.write_bytes(stream(content="TAMPERED"))
    assert not replay(run)["passed"]
    with pytest.raises(ValueError, match="Replay failed"):
        export_runs([run])


def test_missing_key_never_dispatches(project, monkeypatch):
    run = prepare(project, config(project))
    monkeypatch.delenv("ROLLOUT_TEST_KEY")
    with pytest.raises(ValueError, match="environment variable"):
        execute(run)
    assert not (run / "calls").exists()


def test_sse_requires_done_and_ignores_empty_think_wrapper(project):
    run = prepare(project, config(project), limit=1)
    execute(run, lambda r: httpx.Response(200, content=stream(content="<think>\n</think>\nAnswer")))
    assert summarize(run)["status_counts"] == {"success": 1}
    assert parse_sse(stream(done=False))["done"] is False


def test_parquet_export_is_optional(project):
    pq = pytest.importorskip("pyarrow.parquet")
    run = prepare(project, config(project), limit=1)
    execute(run)
    output = Path(export_runs([run], parquet=True)["output"])
    assert pq.read_table(output / "candidates.parquet").to_pylist() == jsonl(output / "candidates.jsonl")
