"""Offline engineering tests. Mock HTTP results are not production model evidence."""
import gzip
import json
from pathlib import Path

import httpx
import pytest

from rlar_harness.config import ModelConfig
from rlar_harness.data_selection import classifier as cl
from rlar_harness.data_selection.models import Classification, SelectionConfig
from rlar_harness.data_selection.pipeline import connect, export, fork_classifier, prepare, run_lock, status
from rlar_harness.data_selection.sources import NearIndex, digest, dumps, group_key, normalize, source_files
from rlar_harness.llm.http_chat_json import HttpChatJsonAdapter

pa = pytest.importorskip("pyarrow")
import pyarrow.parquet as pq


def config(**kw):
    args = dict(targets={"helpsteer2": 2}, candidate_multiplier=3, max_requests=10, concurrency=2,
                near_duplicate_threshold=1.0,
                classifier=ModelConfig(provider_adapter="http_chat_json_v1", endpoint="https://fixture.invalid/v1/chat/completions",
                                       model="gpt-6.1-sol", api_key_env="SELECTION_TEST_KEY", response_format="json_object"))
    args.update(kw)
    return SelectionConfig(**args)


def label(**kw):
    args = dict(decision="include", task_type="advice", task_substance="nontrivial", context_status="complete",
                evaluation_mode="llm_judged", evaluation_dimensions=["relevance"], output_budget_risk="within_or_unknown",
                exclusion_codes=[], evidence="The user asks for practical advice with enough context.")
    args.update(kw)
    return args


def make_sources(project):
    root = project / "codebase/data/datasets"
    def write(name, values):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.suffix == ".parquet":
            pq.write_table(pa.Table.from_pylist(values), p)
        elif p.suffix == ".json":
            p.write_text(json.dumps(values))
        else:
            opener = gzip.open if p.suffix == ".gz" else open
            with opener(p, "wt") as f:
                for v in values:
                    f.write(json.dumps(v) + "\n")
    train = [{"prompt": f"Suggest a useful activity for a neighborhood group focused on topic {i}.", "response": f"SECRET_GOLD_{i}"} for i in range(8)]
    train.append(dict(train[0], response="SECOND_GOLD"))
    write("helpsteer2/train.jsonl.gz", train)
    write("helpsteer2/validation.jsonl.gz", [dict(train[0])])
    for split in ("train", "test"):
        write(f"no_robots/data/{split}-00000-of-00001.parquet", [{"prompt_id": split, "category": "Rewrite",
              "messages": [{"role": "user", "content": "Rewrite this " + split + " sentence with warmth."},
                           {"role": "assistant", "content": "SECRET_DEMONSTRATION"}]}])
    write("ultrafeedback/flan.jsonl", [{"instruction": f"Describe the benefits of activity {i} for friends.", "source": "flan",
                                      "completions": [{"response": "SECRET_GOLD"}]} for i in range(30)])
    write("kodcode/data/train-00000-of-00001.parquet", [{"question_id": "k0", "question": "Write a Python function to count word frequencies.", "subset": "instruct", "style": "instruct"}])
    write("kodcode/data/use_with_caution-00000-of-00001.parquet", [{"question": "EXCLUDED_CAUTION"}])
    for split in ("train", "test"):
        write(f"taco/ALL/{split}-00000-of-00001.parquet", [{"question": f"Compute paths through the {split} graph.", "starter_code": "def solve(graph):", "difficulty": "HARD", "name": split, "source": "fixture"}])
    for split in ("train", "dev", "test", "challenge_test"):
        write(f"mathqa/data/{split}.json", [{"Problem": "Compute " + split, "options": "a) 1 b) 2", "category": "general", "correct": "a", "linear_formula": "SECRET_FORMULA"}])
    write("bigcodebench_hard/data/v0.1.4-00000-of-00001.parquet", [{"task_id": "b0", "instruct_prompt": "Connect a test database.", "complete_prompt": "def task_func(): pass"}])
    write("classeval/data/test-00000-of-00001.parquet", [{"task_id": "c0", "class_description": "Implement a test cache.", "skeleton": "class TestCache: pass"}])
    write("ds1000/test.jsonl", [{"prompt": "Manipulate this test dataframe.", "metadata": {"library": "Pandas", "problem_id": 0}, "reference_code": "SECRET_REFERENCE"}])
    return root


def install_mock(monkeypatch, status=200, text=None):
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        if status != 200:
            return httpx.Response(status, json={"error": {"message": "Unsupported model"}})
        return httpx.Response(200, json={"id": "offline-fixture", "choices": [{"message": {"content": text or json.dumps(label())}, "finish_reason": "stop"}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
    monkeypatch.setenv("SELECTION_TEST_KEY", "private-fixture-key")
    monkeypatch.setattr(cl, "adapter_for", lambda cfg: HttpChatJsonAdapter(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    return seen


def test_prompt_separation_and_family_grouping():
    messages = [{"role": "user", "content": "Tell me a story"}, {"role": "assistant", "content": "Earlier story"},
                {"role": "user", "content": "Make it shorter"}, {"role": "assistant", "content": "FINAL_GOLD"}]
    n = normalize("no_robots", {"messages": messages, "category": "Rewrite"})
    assert n["messages"] == messages[:-1]
    altered = [dict(m) for m in n["messages"]]
    altered[1]["content"] = "A different earlier answer"
    assert group_key(altered) == n["group_id"]
    assert digest(altered) != digest(n["messages"])
    assert "FINAL_GOLD" not in dumps(n)
    assert normalize("helpsteer2", {"prompt": "c#", "response": "GOLD"})["messages"][0]["content"] == "c#"


def test_reference_fields_not_needed_and_starter_code_kept():
    n = normalize("taco", {"question": "Implement this function.", "starter_code": "def solve(xs):", "difficulty": "HARD"})
    assert "def solve(xs):" in n["messages"][0]["content"]
    assert Classification.model_validate(label(evaluation_mode="llm_judged"))
    with pytest.raises(ValueError):
        Classification.model_validate(label(context_status="incomplete"))
    with pytest.raises(ValueError):
        Classification.model_validate(label(decision="exclude", exclusion_codes=["too_hard"]))


def test_near_matching_requires_high_overlap():
    idx = NearIndex(0.85)
    a = "Please explain how a small community garden can improve local relationships and suggest several practical activities for neighbors to try together next spring."
    idx.add("a", idx.features(a))
    assert idx.find(idx.features(a + " Thank you."))[0] == "a"
    assert idx.find(idx.features("Write a sorting algorithm for unsigned integers and carefully describe its computational complexity and memory usage on large arrays.")) is None


def test_prepare_is_deterministic_and_holdouts_win(tmp_path):
    root = make_sources(tmp_path)
    assert not any("use_with_caution" in str(s.path) for s in source_files(root))
    r1, r2 = prepare(tmp_path, config()), prepare(tmp_path, config())
    db = connect(r1)
    assert db.execute("SELECT COUNT(*) FROM records WHERE source='helpsteer2'").fetchone()[0] == 8
    assert db.execute("SELECT COUNT(*) FROM members WHERE source='helpsteer2'").fetchone()[0] == 9
    assert db.execute("SELECT COUNT(*) FROM records WHERE source='helpsteer2' AND disposition='reserved_exact'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM reserved WHERE source='ultrafeedback'").fetchone()[0] > 0
    assert (r1 / "candidates.jsonl").read_text() == (r2 / "candidates.jsonl").read_text()
    assert "SECRET" not in (r1 / "candidates.jsonl").read_text()
    db.close()


def test_real_wire_mock_resume_exports_and_replay(tmp_path, monkeypatch):
    make_sources(tmp_path)
    run = prepare(tmp_path, config())
    seen = install_mock(monkeypatch)
    first = cl.classify(run, max_new_calls=1)
    assert first["selected_total"] == 1 and first["status"] == "incomplete"
    with run_lock(run):
        assert status(run)["accepted_total"] == 1
    second = cl.classify(run)
    assert second["selected_total"] == 2 and second["status"] == "complete"
    cl.classify(run)
    assert len(seen) == 2
    assert all(r["model"] == "gpt-6.1-sol" for r in seen)
    assert "SECRET" not in dumps(seen)
    for path in (run / "requests").rglob("*.json"):
        assert "private-fixture-key" not in path.read_text()
    policy = [json.loads(x) for x in (run / "exports/train_prompts.jsonl").read_text().splitlines()]
    assert all(set(x) == {"sample_id", "data_source", "prompt"} for x in policy)
    assert all(x["prompt"][-1]["role"] == "user" for x in policy)
    assets = [json.loads(x) for x in (run / "exports/evaluation_assets.jsonl").read_text().splitlines()]
    assert {x["sample_id"]: x["selected_prompt_sha256"] for x in assets} == {x["sample_id"]: digest(x["prompt"]) for x in policy}
    assert json.loads((run / "exports/rollout_contract.json").read_text())["max_new_tokens"] == 8000
    assert cl.replay_check(run)["status"] == "passed"


@pytest.mark.parametrize("status,text", [(404, None), (200, "not json")])
def test_service_and_format_errors_are_not_negative_labels(tmp_path, monkeypatch, status, text):
    make_sources(tmp_path)
    run = prepare(tmp_path, config())
    seen = install_mock(monkeypatch, status, text)
    report = cl.classify(run)
    assert len(seen) == (1 if status != 200 else 3) and report["selected_total"] == 0
    assert report["stop_reason"] == "service_or_output_failure"
    report = cl.classify(run)
    assert len(seen) == (1 if status != 200 else 3)  # bounded output repairs, no provider/model fallback
    assert report["stop_reason"] == "existing_service_failure_requires_reconciliation"
    assert cl.replay_check(run)["status"] == "passed"
    db = connect(run)
    assert max(r[0] for r in db.execute("SELECT COUNT(*) FROM calls GROUP BY gid")) <= 3
    assert db.execute("SELECT COUNT(*) FROM records WHERE disposition='exclude'").fetchone()[0] == 0
    db.close()


def test_source_change_refuses_new_calls(tmp_path, monkeypatch):
    root = make_sources(tmp_path)
    run = prepare(tmp_path, config())
    seen = install_mock(monkeypatch)
    (root / "ds1000/test.jsonl").write_text("changed")
    with pytest.raises(ValueError, match="Source file changed"):
        cl.classify(run)
    assert not seen


def test_uncertain_does_not_become_include(tmp_path, monkeypatch):
    make_sources(tmp_path)
    run = prepare(tmp_path, config())
    install_mock(monkeypatch, text=json.dumps(label(decision="uncertain", context_status="unclear")))
    report = cl.classify(run, max_new_calls=1)
    assert report["selected_total"] == 0
    assert len((run / "exports/uncertain.jsonl").read_text().splitlines()) == 1


def test_literal_env_loader_does_not_execute_shell(tmp_path, monkeypatch):
    p = tmp_path / "literal.env"
    p.write_text("SELECTION_LITERAL_TEST='$(touch do-not-create)'\n")
    monkeypatch.delenv("SELECTION_LITERAL_TEST", raising=False)
    cl.load_env_file(p)
    import os
    assert os.environ["SELECTION_LITERAL_TEST"] == "$(touch do-not-create)"
    assert not (tmp_path / "do-not-create").exists()
    monkeypatch.delenv("SELECTION_LITERAL_TEST")


def test_fork_classifier_preserves_parent_failure_and_candidates(tmp_path, monkeypatch):
    make_sources(tmp_path)
    cfg = config()
    run = prepare(tmp_path, cfg)
    install_mock(monkeypatch, status=404)
    cl.classify(run)
    new_cfg = config(classifier=cfg.classifier.model_copy(update={"model": "explicit-new-model"}))
    child = fork_classifier(run, new_cfg)
    parent_db, child_db = connect(run), connect(child)
    assert parent_db.execute("SELECT COUNT(*) FROM calls WHERE status='failed'").fetchone()[0] == 1
    assert child_db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
    assert child_db.execute("SELECT COUNT(*) FROM records WHERE disposition='service_error'").fetchone()[0] == 0
    assert (run / "candidates.jsonl").read_bytes() == (child / "candidates.jsonl").read_bytes()
    parent_db.close()
    child_db.close()
    with pytest.raises(ValueError, match="only permits classifier"):
        fork_classifier(run, new_cfg.model_copy(update={"seed": 123}))


def test_user_supplied_responses_route_preserves_exact_model_and_provider(tmp_path, monkeypatch):
    from rlar_harness.llm.http_responses_json import HttpResponsesJsonAdapter
    cfg = ModelConfig(provider_adapter="http_responses_json_v1", endpoint="https://fixture.invalid/v1/responses",
                      model="us.openai.gpt-6.1-sol", api_key_env="SELECTION_TEST_KEY", auth_provider="aws_third",
                      send_temperature=False, reasoning_effort="low", max_output_tokens=4096, responses_store=False)
    monkeypatch.setenv("SELECTION_TEST_KEY", "private-fixture-key")
    adapter = HttpResponsesJsonAdapter(cfg)
    try:
        assert adapter.build_headers()["authorization"] == "Bearer private-fixture-key?provider=aws_third"
    finally:
        adapter.close()
    assert "private-fixture-key" not in cfg.model_dump_json()


def test_schema_repair_preserves_full_history_and_accepted_cache(tmp_path, monkeypatch):
    make_sources(tmp_path)
    run = prepare(tmp_path, config(targets={"helpsteer2": 1}))
    seen = []
    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        output = label(task_substance="trivial") if len(seen) == 1 else label()
        return httpx.Response(200, json={"id": "mock-repair", "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 15}})
    monkeypatch.setenv("SELECTION_TEST_KEY", "private-fixture-key")
    monkeypatch.setattr(cl, "adapter_for", lambda cfg: HttpChatJsonAdapter(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    report = cl.classify(run)
    assert report["status"] == "complete" and report["selected_total"] == 1
    assert report["calls_by_status"] == {"complete": 1, "invalid_output": 1}
    assert len(seen[0]["messages"]) == 2 and len(seen[1]["messages"]) == 4
    assert seen[1]["messages"][2]["role"] == "assistant"
    assert cl.replay_check(run)["status"] == "passed"
    cl.classify(run)
    assert len(seen) == 2


def test_undispatched_retry_keeps_original_attempt_and_replays(tmp_path, monkeypatch):
    make_sources(tmp_path)
    run = prepare(tmp_path, config(targets={"helpsteer2": 1}))
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            raise httpx.ConnectError("temporary DNS failure")
        return httpx.Response(200, json={"id": "mock-retry", "choices": [{"message": {"content": json.dumps(label())}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 15}})
    monkeypatch.setenv("SELECTION_TEST_KEY", "private-fixture-key")
    monkeypatch.setattr(cl.time, "sleep", lambda _: None)
    monkeypatch.setattr(cl, "adapter_for", lambda cfg: HttpChatJsonAdapter(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    report = cl.classify(run)
    assert report["status"] == "complete"
    assert report["calls_by_status"] == {"complete": 1, "failed": 1}
    assert report["usage"]["undispatched_attempts"] == 1
    assert seen[0] == seen[1]
    assert cl.replay_check(run)["verified_records"] == 2


def test_dispatched_timeout_is_not_retried(tmp_path, monkeypatch):
    make_sources(tmp_path)
    run = prepare(tmp_path, config())
    seen = []
    def handler(request):
        seen.append(1)
        raise httpx.ReadTimeout("unknown outcome")
    monkeypatch.setenv("SELECTION_TEST_KEY", "private-fixture-key")
    monkeypatch.setattr(cl, "adapter_for", lambda cfg: HttpChatJsonAdapter(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    report = cl.classify(run)
    assert len(seen) == 1 and report["calls_by_status"] == {"unknown": 1}
    cl.classify(run)
    assert len(seen) == 1


def test_transport_retry_followed_by_schema_repair(tmp_path, monkeypatch):
    make_sources(tmp_path)
    run = prepare(tmp_path, config(targets={"helpsteer2": 1}))
    seen = []
    def handler(request):
        seen.append(json.loads(request.content))
        if len(seen) == 1:
            raise httpx.ConnectError("temporary DNS failure")
        output = label(task_substance="trivial") if len(seen) == 2 else label()
        return httpx.Response(200, json={"id": "mock-mixed", "choices": [{"message": {"content": json.dumps(output)}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 15}})
    monkeypatch.setenv("SELECTION_TEST_KEY", "private-fixture-key")
    monkeypatch.setattr(cl.time, "sleep", lambda _: None)
    monkeypatch.setattr(cl, "adapter_for", lambda cfg: HttpChatJsonAdapter(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    report = cl.classify(run)
    assert report["status"] == "complete" and len(seen) == 3
    assert cl.replay_check(run)["verified_records"] == 3


def test_rate_pacing_obeys_sliding_window(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cl.time, "time", lambda: clock[0])
    monkeypatch.setattr(cl.time, "sleep", lambda t: clock.__setitem__(0, clock[0] + t))
    recent = [80.0, 90.0]
    cl.pace_requests(recent, 1, 2)
    assert clock[0] >= 140.0 and recent == [90.0]


def test_explicit_503_retry_keeps_unknown_usage(tmp_path, monkeypatch):
    make_sources(tmp_path)
    run = prepare(tmp_path, config(targets={"helpsteer2": 1}))
    seen = []
    def handler(request):
        seen.append(1)
        if len(seen) == 1:
            return httpx.Response(503, json={"error": {"type": "server_error"}})
        return httpx.Response(200, json={"id": "mock-503-retry", "choices": [{"message": {"content": json.dumps(label())}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 12, "completion_tokens": 15}})
    monkeypatch.setenv("SELECTION_TEST_KEY", "private-fixture-key")
    monkeypatch.setattr(cl.time, "sleep", lambda _: None)
    monkeypatch.setattr(cl, "adapter_for", lambda cfg: HttpChatJsonAdapter(cfg, client=httpx.Client(transport=httpx.MockTransport(handler))))
    report = cl.classify(run)
    assert report["status"] == "complete" and len(seen) == 2
    assert report["usage"]["unknown_usage_calls"] == 1
    assert cl.replay_check(run)["verified_records"] == 2
