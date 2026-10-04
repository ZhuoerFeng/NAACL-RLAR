"""One retry owner, bounded dispatch, durable attempts, and explicit uncertainty."""
import asyncio
import json
import os
import time
from pathlib import Path

import httpx

from ..storage.lock import RunDirLock
from .store import attempts, digest, dumps, latest, load_run, now, read, summarize, tasks, write
from .stream import RedactedStream, classify, parse_sse, reasoning_observed


async def call_once(client, run, task, cfg, token, *, evidence_kind="real_http"):
    number = len(attempts(run, task)) + 1
    call = Path(run) / "calls" / task["rollout_id"] / f"{number:04d}"
    call.mkdir(parents=True)  # reservation survives crashes; never overwrite an attempt
    request = cfg.generation_parameters(task["sample_index"])
    request.update(messages=task["prompt"], request_id=f"rlar_{task['rollout_id']}_{number}")
    if token in dumps(request):
        raise ValueError("Credential appears in request payload; refusing to persist it")
    write(call / "request.json", request)
    write(call / "started.json", {"started_at": now(), "attempt": number,
                                 "request_id": request["request_id"], "endpoint": cfg.endpoint,
                                 "evidence_kind": evidence_kind})
    start = time.monotonic()
    result = {"attempt": number, "http_status": None, "transport_error": None, "evidence_kind": evidence_kind,
              "first_byte_seconds": None, "retry_after_seconds": 0}
    headers = {}
    with (call / "response.sse").open("wb") as file:
        sink = RedactedStream(file, token)
        try:
            async with asyncio.timeout(cfg.request_timeout_seconds):
                async with client.stream("POST", cfg.endpoint, content=dumps(request).encode(), headers={
                    "Content-Type": "application/json", "Authorization": "Bearer " + token,
                }) as response:
                    result["http_status"] = response.status_code
                    result["headers_seconds"] = round(time.monotonic() - start, 4)
                    headers = {k: v.replace(token, "[REDACTED]") for k, v in response.headers.items()
                               if k.lower() not in ("authorization", "proxy-authorization", "set-cookie")}
                    write(call / "response_headers.json", headers)
                    try:
                        result["retry_after_seconds"] = min(3600, max(0, float(headers.get("retry-after", "0"))))
                    except ValueError:
                        pass
                    async for chunk in response.aiter_bytes():
                        if result["first_byte_seconds"] is None:
                            result["first_byte_seconds"] = round(time.monotonic() - start, 4)
                        sink.feed(chunk)
        except (httpx.HTTPError, TimeoutError) as exc:
            # Exception strings may contain proxy credentials or request details.
            result["transport_error"] = type(exc).__name__
        finally:
            sink.feed(b"", final=True)
            os.fsync(file.fileno())
    raw = (call / "response.sse").read_bytes()
    parsed = parse_sse(raw)
    result.update(parsed)
    result.update(elapsed_seconds=round(time.monotonic() - start, 4),
                  status=classify(parsed, http_status=result["http_status"],
                                  transport_error=result["transport_error"], model=cfg.model, thinking=cfg.thinking),
                  reasoning_observed=reasoning_observed(parsed), credential_echo_redacted=sink.redacted,
                  usage_complete=bool(result["http_status"] == 200 and not result["transport_error"]
                                      and parsed["done"] and parsed["finish_reason"] and not parsed["errors"]
                                      and parsed["usage"] and all(key in parsed["usage"] for key in
                                          ("prompt_tokens", "completion_tokens", "total_tokens"))),
                  request_sha256=digest((call / "request.json").read_bytes()), response_sha256=digest(raw))
    message = {"role": "assistant", "content": parsed["content"],
               "reasoning_content": parsed["reasoning_content"], "refusal": parsed["refusal"]}
    write(call / "history.json", {"messages": task["prompt"] + [message], "tool_observations": []})
    # result.json is the commit marker, written only after all response/history data.
    write(call / "result.json", result)
    return result


async def sample(run, *, max_new_calls=None, retry_uncertain=False, retry_failed=False, transport=None):
    """transport is an offline-test injection; the production CLI has no mock switch."""
    if max_new_calls is not None and max_new_calls < 1:
        raise ValueError("--max-new-calls must be positive")
    with RunDirLock(Path(run)):
        manifest, cfg, prompts = load_run(run, for_sampling=True)
        token = os.environ.get(cfg.api_key_env, "")
        if not token or "\n" in token or "\r" in token:
            raise ValueError(f"Set runtime environment variable {cfg.api_key_env}")
        all_tasks = list(tasks(manifest, cfg, prompts))
        reserved = sum(len(attempts(run, task)) for task in all_tasks)
        existing = {latest(run, task)["status"] for task in all_tasks}
        blocked = existing & {"thinking_violation", "model_mismatch"}
        if not retry_failed:
            blocked |= existing & {"failed", "protocol_error"}
        if blocked:
            return {**summarize(run), "new_calls": 0, "stop_reason": "service_contract_failure",
                    "blocking_statuses": sorted(blocked)}
        queue = asyncio.Queue()
        for task in all_tasks:
            status = latest(run, task)["status"]
            eligible = status in ("pending", "retryable") or (status == "uncertain" and retry_uncertain)
            eligible |= retry_failed and status in ("failed", "protocol_error", "empty", "unsupported_finish")
            if eligible and len(attempts(run, task)) < cfg.max_attempts:
                queue.put_nowait(task)
        dispatched, stop_reason, next_start = 0, None, 0.0
        gate = asyncio.Lock()

        async def reserve_slot():
            nonlocal dispatched, next_start, stop_reason
            async with gate:
                if stop_reason:
                    return False
                if reserved + dispatched >= cfg.max_requests:
                    stop_reason = "request_budget_exhausted"
                    return False
                if max_new_calls is not None and dispatched >= max_new_calls:
                    stop_reason = "max_new_calls_reached"
                    return False
                await asyncio.sleep(max(0, next_start - time.monotonic()))
                if stop_reason:
                    return False
                next_start = time.monotonic() + 60 / cfg.requests_per_minute
                dispatched += 1
                return True

        async def worker(client):
            nonlocal stop_reason
            while not queue.empty() and not stop_reason:
                task = queue.get_nowait()
                previous = latest(run, task)
                while len(attempts(run, task)) < cfg.max_attempts:
                    if previous["status"] == "retryable":
                        delay = max(previous.get("retry_after_seconds", 0),
                                    cfg.retry_backoff_seconds * 2 ** max(0, len(attempts(run, task)) - 1))
                        await asyncio.sleep(delay)
                    if not await reserve_slot():
                        break
                    result = await call_once(client, run, task, cfg, token,
                                             evidence_kind="offline_fixture" if transport else "real_http")
                    print(json.dumps({"sample_id": task["sample_id"], "sample_index": task["sample_index"],
                                      "attempt": result["attempt"], "status": result["status"],
                                      "http_status": result["http_status"], "elapsed_seconds": result["elapsed_seconds"]}), flush=True)
                    if result["status"] in ("thinking_violation", "model_mismatch", "protocol_error", "failed"):
                        stop_reason = "service_contract_failure"
                    elif result["status"] == "uncertain":
                        stop_reason = "uncertain_request_requires_review"
                    elif result["status"] == "retryable" and len(attempts(run, task)) >= cfg.max_attempts:
                        stop_reason = "service_retries_exhausted"
                    if result["status"] != "retryable":
                        break
                    previous = result

        async with httpx.AsyncClient(transport=transport, follow_redirects=False,
                                     timeout=httpx.Timeout(cfg.request_timeout_seconds, connect=20),
                                     limits=httpx.Limits(max_connections=cfg.concurrency)) as client:
            workers = [asyncio.create_task(worker(client)) for _ in range(cfg.concurrency)]
            try:
                await asyncio.gather(*workers)
            finally:
                for worker_task in workers:
                    if not worker_task.done():
                        worker_task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
        summary = summarize(run)
        summary.update(new_calls=dispatched, stop_reason=stop_reason, updated_at=now())
        write(Path(run) / "summary.json", summary)
        return summary


def replay(run):
    run = Path(run).resolve()
    manifest, cfg, prompts = load_run(run)
    checked, incomplete, failures = 0, [], []
    for task in tasks(manifest, cfg, prompts):
        for call in attempts(run, task):
            relative = str(call.relative_to(run))
            if not (call / "result.json").exists():
                incomplete.append(relative)
                continue
            try:
                result = read(call / "result.json")
                started = read(call / "started.json")
                request = read(call / "request.json")
                expected_request = cfg.generation_parameters(task["sample_index"])
                expected_request.update(messages=task["prompt"],
                                        request_id=f"rlar_{task['rollout_id']}_{int(call.name)}")
                if request != expected_request:
                    raise ValueError("request_binding")
                if (started["evidence_kind"] != result["evidence_kind"] or started["endpoint"] != cfg.endpoint
                        or started["request_id"] != request["request_id"] or result["attempt"] != int(call.name)):
                    raise ValueError("attempt_binding")
                raw = (call / "response.sse").read_bytes()
                if result["request_sha256"] != digest((call / "request.json").read_bytes()) or result["response_sha256"] != digest(raw):
                    raise ValueError("request_or_response_hash")
                parsed = parse_sse(raw)
                if any(result[key] != value for key, value in parsed.items()):
                    raise ValueError("sse_replay")
                if result["status"] != classify(parsed, http_status=result["http_status"],
                                                transport_error=result["transport_error"], model=cfg.model, thinking=cfg.thinking):
                    raise ValueError("status_binding")
                history = read(call / "history.json")
                expected_message = {"role": "assistant", **{k: parsed[k] for k in ("content", "reasoning_content", "refusal")}}
                if history != {"messages": request["messages"] + [expected_message], "tool_observations": []}:
                    raise ValueError("history_binding")
                checked += 1
            except (KeyError, ValueError, OSError) as exc:
                failures.append({"call": relative, "error": type(exc).__name__})
    return {"schema_version": "rlar.rollout.replay.v1", "checked_attempts": checked,
            "incomplete_attempts": incomplete, "failures": failures, "passed": not failures,
            "scope": "Offline evidence integrity, not model quality or platform thinking-control certification"}
