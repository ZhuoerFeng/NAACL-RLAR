"""Real classifier requests with frozen prompts, durable traces and no model fallback."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from pydantic import ValidationError

from ..llm.adapter import ProviderError
from ..llm.http_chat_json import HttpChatJsonAdapter
from ..llm.http_responses_json import HttpResponsesJsonAdapter
from ..schemas import LLMRequest, Message
from .models import Classification
from .pipeline import (accepted_counts, apply_label, check_inputs, connect, export, extend_pool,
                       next_batch, run_config, write_json)
from .sources import digest, dumps


def load_env_file(path):
    """Load literal dotenv assignments without shell execution or logging values."""
    if path is None:
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not name.replace("_", "a").isalnum():
            raise ValueError("Unsupported dotenv syntax")
        if value.startswith(("'", '"')) and value[-1:] == value[:1]:
            value = value[1:-1]
        if name not in os.environ:
            os.environ[name] = value


def scrub(obj, secret):
    if isinstance(obj, str):
        return obj.replace(secret, "[REDACTED]") if secret else obj
    if isinstance(obj, dict):
        return {k: scrub(v, secret) for k, v in obj.items()}
    if isinstance(obj, list):
        return [scrub(v, secret) for v in obj]
    return obj


def adapter_for(config):
    cls = HttpResponsesJsonAdapter if config.provider_adapter == "http_responses_json_v1" else HttpChatJsonAdapter
    return cls(config)


MAX_OUTPUT_REPAIRS = 2
MAX_TRANSPORT_RETRIES = 2


def repair_history(run, db, gid, keys=None):
    if keys is None:
        calls = db.execute("SELECT cache_key,response_path FROM calls WHERE gid=? AND status='invalid_output' ORDER BY rowid", (gid,)).fetchall()
    else:
        calls = [db.execute("SELECT cache_key,response_path FROM calls WHERE cache_key=? AND gid=? AND status='invalid_output'", (key, gid)).fetchone() for key in keys]
        if any(c is None for c in calls):
            raise ValueError("Repair history references an invalid prior call")
    return [(c["cache_key"], json.loads((run / c["response_path"]).read_text())) for c in calls]


def queue_output_repairs(db):
    for row in db.execute("SELECT gid FROM records WHERE disposition='service_error'").fetchall():
        calls = db.execute("SELECT status FROM calls WHERE gid=? ORDER BY rowid", (row["gid"],)).fetchall()
        if calls and calls[-1]["status"] == "invalid_output" and sum(c["status"] == "invalid_output" for c in calls) <= MAX_OUTPUT_REPAIRS:
            db.execute("UPDATE records SET disposition='pending',detail='Bounded schema correction; original responses retained' WHERE gid=?", (row["gid"],))
    db.commit()


def queue_transport_retries(run, db):
    """Retry confirmed connection/rate errors and explicit HTTP 5xx failures.

    Unanswered dispatched requests remain unknown; they are never retried here.
    HTTP 5xx usage remains unknown even when its classification failure is known.
    """
    queued, wait = [], 0.0
    for row in db.execute("SELECT gid FROM records WHERE disposition='service_error'").fetchall():
        calls = db.execute("SELECT status,response_path FROM calls WHERE gid=? ORDER BY rowid", (row["gid"],)).fetchall()
        if not calls or calls[-1]["status"] != "failed" or not calls[-1]["response_path"]:
            continue
        last = json.loads((run / calls[-1]["response_path"]).read_text())
        failed_count = sum(c["status"] == "failed" for c in calls)
        safe_connection_retry = last.get("dispatched") is False and last.get("code") in ("connection_lost", "http_rate_limited")
        explicit_server_failure = last.get("code") == "http_server_error" and 500 <= (last.get("status_code") or 0) < 600
        if (safe_connection_retry or explicit_server_failure) and failed_count <= MAX_TRANSPORT_RETRIES:
            delay = max(2.0 ** failed_count, last.get("retry_after_s") or 0)
            if last.get("status_code") == 429:
                delay = max(60.0, delay)
            if explicit_server_failure:
                delay = max(10.0 * failed_count, delay)
            wait = max(wait, delay)
            queued.append(row["gid"])
    while wait > 0:
        chunk = min(wait, 30)
        time.sleep(chunk)
        wait -= chunk
    for gid in queued:
        db.execute("UPDATE records SET disposition='pending',detail='Bounded retry of a known transport failure; original attempts retained' WHERE gid=?", (gid,))
    db.commit()


def physical_key(request_key, transport_attempt):
    return request_key if transport_attempt == 0 else digest({"request_key": request_key, "transport_attempt": transport_attempt})


def pace_requests(recent, batch_size, rpm):
    while True:
        now = time.time()
        recent[:] = [t for t in recent if now - t < 60]
        if len(recent) + batch_size <= rpm:
            return
        release = sorted(recent)[len(recent) + batch_size - rpm - 1] + 60.05
        time.sleep(min(30.0, max(0.05, release - now)))


def build_request(run, cfg, row, history=()):
    system = (run / "classifier_prompt.txt").read_text()
    system += "\nJSON schema:\n" + dumps(Classification.model_json_schema())
    payload = {"conversation": json.loads(row["messages"]), "next_action": "generate the next assistant answer"}
    request = LLMRequest(episode_id=row["gid"], expected_history_cursor=0,
                         prefix_ref="classifier_prompt.txt", history_ref=row["gid"], model_config_ref="config.json",
                         logical_call_id=row["gid"], remaining_budget={"max_requests": cfg.max_requests},
                         max_output_tokens=cfg.classifier.max_output_tokens, temperature=cfg.classifier.temperature,
                         messages=[Message(role="system", content=system, actor="run_prefix"),
                                   Message(role="user", content=dumps(payload), actor="episode_prefix")])
    for _, prior in history:
        request.messages.extend([
            Message(role="assistant", content=prior["normalized"]["text"], actor="assistant"),
            Message(role="user", actor="harness_observation", content=(
                "Your previous classifier JSON failed validation: " + prior["error"] +
                "\nReturn a corrected complete JSON object, not a solution to the task. "
                "Consistency rules: include requires context_status=complete, task_substance=nontrivial, "
                "nonempty evaluation_dimensions, no exclusion_codes and no explicit output budget violation. "
                "Exclude requires an allowed concrete exclusion_code. Here nontrivial means substantive, "
                "not difficult: an easy factual question may still be substantive. Never reject a task merely "
                "because it is easy, subjective, or has no reference answer. Use uncertain if genuinely ambiguous.")),
        ])
    adapter = adapter_for(cfg.classifier)
    try:
        body = adapter.build_body(request)
    finally:
        adapter.close()
    key = digest({"endpoint": cfg.classifier.endpoint, "provider": cfg.classifier.auth_provider,
                  "request": body, "schema": Classification.model_json_schema()})
    return key, request, body


def parse_label(result):
    if result.finish_reason != "stop":
        raise ValueError("Incomplete classifier output, not an exclusion")
    return Classification.model_validate(json.loads(result.text))


def execute(run, cfg, row, key, request, body):
    folder = run / "requests" / key
    secret = os.environ.get(cfg.classifier.api_key_env or "", "")
    adapter = adapter_for(cfg.classifier)
    started = time.monotonic()
    try:
        result = adapter.send_prepared(body, request, attempt=1)
        response = {"normalized": asdict(result), "profile": "real_service"}
        try:
            label = parse_label(result)
        except (ValueError, ValidationError) as exc:
            response.update(status="invalid_output", error=type(exc).__name__ + ": " + str(exc))
            label = None
        else:
            response.update(status="complete", classification=label.model_dump(mode="json"))
    except ProviderError as exc:
        # A received HTTP error has a known failure; an unanswered dispatched request remains unknown.
        response = {"status": "unknown" if exc.dispatched and not getattr(exc, "raw", None) else "failed",
                    "profile": "real_service", "error": str(exc), "code": str(exc.code),
                    "dispatched": exc.dispatched, "status_code": exc.status_code,
                    "retryable": exc.retryable, "retry_after_s": exc.retry_after_s,
                    "raw": getattr(exc, "raw", None), "usage_known": False}
        label = None
    except Exception as exc:
        response = {"status": "unknown", "profile": "real_service", "error": type(exc).__name__ + ": " + str(exc), "usage_known": False}
        label = None
    finally:
        adapter.close()
    response = scrub(response, secret)
    response["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    response["elapsed_seconds"] = round(time.monotonic() - started, 3)
    write_json(folder / "response.json", response)
    return row, key, response, label


def classify(run, max_new_calls=None, requests_per_minute=None):
    cfg, state = run_config(run)
    check_inputs(run)
    secret = os.environ.get(cfg.classifier.api_key_env or "")
    if cfg.classifier.api_key_env and not secret:
        write_json(run / "preflight_failure.json", {"error": "missing_runtime_credential", "env_name": cfg.classifier.api_key_env,
                                                    "model": cfg.classifier.model, "requests_sent": 0})
        raise RuntimeError("Classifier credential is not present at runtime")
    db = connect(run)
    limits_path = run / "transport_limits.json"
    limits = json.loads(limits_path.read_text()) if limits_path.exists() else {"requests_per_minute": 50}
    if requests_per_minute is not None:
        if not 1 <= requests_per_minute <= 60:
            raise ValueError("requests_per_minute must be between 1 and the observed project limit of 60")
        limits["requests_per_minute"] = requests_per_minute
    write_json(limits_path, limits)
    recent = []
    for call in db.execute("SELECT request_path FROM calls"):
        path = run / call["request_path"] / "request.json"
        req = json.loads(path.read_text())
        ts = req.get("dispatched_at_utc")
        recent.append(datetime.fromisoformat(ts).timestamp() if ts else path.stat().st_mtime)
    invocation = {"started_at_utc": datetime.now(timezone.utc).isoformat(), "max_new_calls": max_new_calls,
                  "requests_per_minute": limits["requests_per_minute"], "max_output_repairs": MAX_OUTPUT_REPAIRS,
                  "max_transport_retries": MAX_TRANSPORT_RETRIES,
                  "code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    with (run / "invocations.jsonl").open("a") as f:
        f.write(dumps(invocation) + "\n")
    # An interrupted dispatch is never silently retried. Restore from a complete durable response if possible.
    for call in db.execute("SELECT * FROM calls WHERE status='started'").fetchall():
        response_path = run / call["request_path"] / "response.json"
        if response_path.exists():
            response = json.loads(response_path.read_text())
            if response.get("status") == "complete":
                label = Classification.model_validate(response["classification"])
                row = dict(db.execute("SELECT * FROM records WHERE gid=?", (call["gid"],)).fetchone())
                db.execute("UPDATE calls SET status='complete',label=?,response_path=? WHERE cache_key=?",
                           (dumps(label.model_dump(mode="json")), str(response_path.relative_to(run)), call["cache_key"]))
                apply_label(db, row, label, cfg)
                continue
        db.execute("UPDATE calls SET status='unknown',error='Interrupted dispatch; outcome needs reconciliation' WHERE cache_key=?", (call["cache_key"],))
        db.execute("UPDATE records SET disposition='service_error' WHERE gid=?", (call["gid"],))
    db.commit()
    queue_output_repairs(db)
    queue_transport_retries(run, db)
    failures = db.execute("SELECT COUNT(*) FROM records WHERE disposition='service_error'").fetchone()[0]
    if failures:
        db.close()
        report = export(run)
        report["stop_reason"] = "existing_service_failure_requires_reconciliation"
        write_json(run / "selection_report.json", report)
        return report
    used = db.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
    new_calls = 0
    stop = "target_reached"
    while True:
        accepted = accepted_counts(db)
        need_sources = [s for s, target in cfg.targets.items() if accepted.get(s, 0) < target]
        if not need_sources:
            break
        allowance = cfg.max_requests - used
        if max_new_calls is not None:
            allowance = min(allowance, max_new_calls - new_calls)
        if allowance <= 0:
            stop = "invocation_call_limit" if max_new_calls is not None and new_calls >= max_new_calls else "run_request_budget"
            break
        # First request is a real preflight; avoid spending a whole batch on an unsupported model.
        limits = json.loads(limits_path.read_text())
        rpm = limits["requests_per_minute"]
        if type(rpm) is not int or not 1 <= rpm <= 60:
            raise ValueError("Invalid live requests_per_minute limit")
        size = 1 if db.execute("SELECT COUNT(*) FROM calls WHERE status='complete'").fetchone()[0] == 0 else min(cfg.concurrency, allowance, rpm)
        batch = next_batch(db, cfg, size)
        if not batch:
            if not extend_pool(run, db, cfg, need_sources):
                stop = "eligible_candidates_exhausted"
                break
            batch = next_batch(db, cfg, size)
        jobs = []
        pace_requests(recent, len(batch), rpm)
        for row in batch:
            history = repair_history(run, db, row["gid"])
            request_key, request, body = build_request(run, cfg, row, history)
            transport_attempt = db.execute("SELECT COUNT(*) FROM calls WHERE gid=? AND status='failed'", (row["gid"],)).fetchone()[0]
            key = physical_key(request_key, transport_attempt)
            cached = db.execute("SELECT * FROM calls WHERE cache_key=?", (key,)).fetchone()
            if cached:
                raise RuntimeError("Unexpected cached pending row; resume requires reconciliation")
            folder = run / "requests" / key
            folder.mkdir(parents=True, exist_ok=False)
            write_json(folder / "request.json", scrub({"profile": "real_service", "endpoint": cfg.classifier.endpoint,
                       "model": cfg.classifier.model, "sample_id": row["gid"], "body": body,
                       "cache_key": key, "headers_persisted": False,
                       "request_key": request_key, "transport_attempt": transport_attempt,
                       "repair_history_keys": [item[0] for item in history],
                       "dispatched_at_utc": datetime.now(timezone.utc).isoformat()}, secret))
            db.execute("INSERT INTO calls(cache_key,gid,status,request_path) VALUES(?,?,'started',?)",
                       (key, row["gid"], str(folder.relative_to(run))))
            db.commit()
            recent.append(time.time())
            jobs.append((row, key, request, body))
        with ThreadPoolExecutor(max_workers=size) as pool:
            futures = [pool.submit(execute, run, cfg, *job) for job in jobs]
            for future in futures:
                row, key, response, label = future.result()
                db.execute("UPDATE calls SET status=?,response_path=?,label=?,error=? WHERE cache_key=?",
                           (response["status"], f"requests/{key}/response.json",
                            dumps(label.model_dump(mode="json")) if label else None,
                            response.get("error"), key))
                if label:
                    apply_label(db, row, label, cfg)
                else:
                    db.execute("UPDATE records SET disposition='service_error',detail=? WHERE gid=?", (response.get("error"), row["gid"]))
                db.commit()
                new_calls += 1
                used += 1
        progress = {"new_calls": new_calls, "total_calls": used, "accepted_by_source": accepted_counts(db),
                    "model": cfg.classifier.model, "requests_per_minute": rpm}
        write_json(run / "classifier_progress.json", progress)
        print(dumps(progress), flush=True)
        queue_output_repairs(db)
        queue_transport_retries(run, db)
        if db.execute("SELECT COUNT(*) FROM records WHERE disposition='service_error'").fetchone()[0]:
            stop = "service_or_output_failure"
            break
    db.close()
    report = export(run)
    report["stop_reason"] = stop
    write_json(run / "selection_report.json", report)
    return report


def replay_check(run):
    cfg, _ = run_config(run)
    db = connect(run)
    verified, failures = 0, []
    for call in db.execute("SELECT * FROM calls ORDER BY cache_key"):
        try:
            row = dict(db.execute("SELECT * FROM records WHERE gid=?", (call["gid"],)).fetchone())
            request = json.loads((run / call["request_path"] / "request.json").read_text())
            history = repair_history(run, db, call["gid"], request.get("repair_history_keys", []))
            request_key, _, body = build_request(run, cfg, row, history)
            key = physical_key(request_key, request.get("transport_attempt", 0))
            if key != call["cache_key"] or body != request["body"]:
                raise ValueError("Recorded request differs from frozen prompt/model/input")
            if call["status"] == "complete":
                response = json.loads((run / call["response_path"]).read_text())
                # Reparse original provider JSON through the same adapter without any network request.
                import httpx
                raw = response["normalized"]["raw"]
                adapter = adapter_for(cfg.classifier)
                try:
                    parsed = adapter.parse_response(httpx.Response(raw["status_code"], text=raw["text"]))
                finally:
                    adapter.close()
                label = parse_label(parsed).model_dump(mode="json")
                if label != json.loads(call["label"]):
                    raise ValueError("Replayed classification differs")
            elif call["response_path"]:
                json.loads((run / call["response_path"]).read_text())
            verified += 1
        except Exception as exc:
            failures.append({"cache_key": call["cache_key"], "error": str(exc)})
    report = {"status": "passed" if not failures else "failed", "verified_records": verified, "failures": failures,
              "meaning": "Trace integrity and deterministic offline parsing only; recorded service failures remain failures.",
              "service_statuses": dict(db.execute("SELECT status,COUNT(*) FROM calls GROUP BY status").fetchall())}
    write_json(run / "replay_report.json", report)
    db.close()
    return report
