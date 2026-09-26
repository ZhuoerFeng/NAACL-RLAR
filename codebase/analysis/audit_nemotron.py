#!/usr/bin/env python3
"""Full structural audit plus a seeded diagnostic reservoir, not final sampling.

Task hints are transparent keyword heuristics, NOT official semantic labels.
The requested final 1,000-row sample is deliberately not produced here.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import re
import time
import unicodedata

import numpy as np
import orjson
import pyarrow as pa
import pyarrow.parquet as pq

BASE = Path(__file__).resolve().parents[1]
RAW = BASE / "data/raw/nemotron-instruction-following-chat-v2"
OUT = BASE / "analysis/results/nemotron_sampling_review"
SEED, RESERVOIR_SIZE = 20260923, 8192
RULES = [
    ("translation", r"\btranslat(?:e|ion|ing)\b|翻译"),
    ("summarization", r"\bsummari[sz](?:e|ation|ing)\b|\b(?:give|write|provide|make)\b.{0,35}\bsummary\b|总结|摘要"),
    ("rewrite_edit", r"\b(?:rewrite|rephrase|proofread|paraphrase|polish)\b|\b(?:correct|fix|improve)\b.{0,45}\b(?:grammar|sentence|wording|text|paragraph)\b|润色|改写"),
    ("code_technical", r"```|\b(?:python|javascript|typescript|java|c\+\+|csharp|sql|html|css|debug|compiler|compiling|linux|powershell|autohotkey|code|function|algorithm|programming|script|regex|bash|api)\b|编程|代码"),
    ("math_quantitative", r"\\(?:frac|sqrt|int|sum|begin)|\b(?:equation|calculate|integral|derivative|probability|polynomial|theorem|algebra|geometry|arithmetic|solve for|how many|how much)\b|计算|方程"),
    ("extraction_format", r"\b(?:extract|classify|categorize|json|csv|xml|tabulate)\b|\b(?:format|convert|sort)\b.{0,50}\b(?:table|list|data|text|following|these)\b|提取|分类"),
    ("creative_generation", r"\b(?:write|create|compose|draft|generate)\b.{0,90}\b(?:story|poem|song|essay|email|letter|dialogue|speech|scene|narrative|fiction|article|blog|slogan)\b|写作|写一"),
    ("planning_advice", r"\b(?:recommend|advice|suggest|planning|plan|strategy|strategies|tips|brainstorm)\b|\bhow (?:can|do|should|would) (?:i|we|you)\b|建议|计划"),
    ("knowledge_explanation", r"\b(?:what|why|who|when|where|which|explain|describe|define|compare|difference|tell me|discuss)\b|什么|为何|为什么|解释"),
]
PATTERNS = [(name, re.compile(pattern, re.I | re.S)) for name, pattern in RULES]
QA_MARKERS = re.compile(r"(?:^|\n)\s*(?:question|answer|q|a)\s*:", re.I)
NON_LATIN = re.compile(r"[\u0400-\u04ff\u0600-\u06ff\u0900-\u0dff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def iter_lines(section):
    """Read finished files or only completed ranges of the active download."""
    target = RAW / "data" / f"{section}.jsonl"
    if target.exists():
        with target.open("rb", buffering=4 * 1024 * 1024) as stream:
            yield from stream
        return
    state_path = target.with_suffix(".ranges.json")
    partial = target.with_suffix(".ranges.partial")
    entry = next(x for x in json.loads((RAW / "upstream_tree.json").read_text()) if x["path"] == f"data/{section}.jsonl")
    state = json.loads(state_path.read_text())
    chunk_size = state["chunk_bytes"]
    try:
        fd = os.open(partial, os.O_RDONLY)
    except FileNotFoundError:
        fd = os.open(target, os.O_RDONLY)
    carry = b""
    try:
        for idx in range((entry["size"] + chunk_size - 1) // chunk_size):
            while idx not in state["complete"]:
                time.sleep(5)
                state = json.loads(state_path.read_text())
            block = os.pread(fd, min(chunk_size, entry["size"] - idx * chunk_size), idx * chunk_size)
            pieces = (carry + block).split(b"\n")
            carry = pieces.pop()
            for piece in pieces:
                yield piece + b"\n"
        if carry:
            yield carry
    finally:
        os.close(fd)


def norm(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def fingerprint(messages):
    cleaned = [(m.get("role"), norm(m.get("content", ""))) for m in messages
               if isinstance(m.get("content"), str) and not (m.get("role") == "system" and not m["content"].strip())]
    return hashlib.sha256(orjson.dumps(cleaned)).digest()


def stats(values):
    a = np.asarray(values)
    return {"n": len(a), "mean": float(a.mean()), **{k: float(np.quantile(a, q)) for k, q in [("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99), ("max", 1)]}} if len(a) else {}


def scan():
    OUT.mkdir(parents=True, exist_ok=True)
    manifest_path = RAW / "download_manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"verification": "Download in progress; completed ranges only, full checksums required before final report"}
    existing = set()
    for split in ["train", "valid"]:
        for row in pq.read_table(BASE / "data/cleaned" / f"{split}.parquet", columns=["prompt"]).to_pylist():
            existing.add(fingerprint(row["prompt"]))
    result = {"created_utc": datetime.now(timezone.utc).isoformat(), "download": manifest,
              "diagnostic_reservoir_per_section": RESERVOIR_SIZE, "seed": SEED,
              "task_hint_method": "first matching regex over first 4,000 characters of the last user message; if that message has fewer than 80 characters, append the first user message. Heuristic, multi-label signals retained; not a semantic classifier.",
              "task_hint_rules": RULES, "sections": {}}
    prompt_counts, seed_counts, uuid_counts, reservoir_all, index_batch, writer = {}, {}, {}, [], [], None
    diagnostic_examples = []
    for section in ["reasoning_off", "reasoning_on"]:
        rng = random.Random(SEED + (section == "reasoning_on"))
        path = RAW / "data" / f"{section}.jsonl"
        counters = {name: Counter() for name in ["top_level_keys", "message_keys", "roles", "reasoning", "used_in", "license", "user_turns", "task_hints", "task_signals", "flags"]}
        prompts, seeds, uuids, reference_hashes = Counter(), Counter(), Counter(), Counter()
        pchars, achars, rchars, reservoir, offset, bad_json = [], [], [], [], 0, []
        strict_good = 0
        if True:
            stream = iter_lines(section)
            for idx, line in enumerate(stream):
                position, offset = offset, offset + len(line)
                try:
                    row = orjson.loads(line)
                except orjson.JSONDecodeError:
                    bad_json.append({"row": idx, "offset": position})
                    continue
                counters["top_level_keys"].update(row.keys())
                counters["reasoning"][str(row.get("reasoning"))] += 1
                counters["used_in"].update(row.get("used_in") or [])
                counters["license"][str(row.get("license"))] += 1
                flags = []
                messages = row.get("messages") or []
                if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
                    messages = []
                    flags.append("invalid_messages_structure")
                for msg in messages:
                    counters["message_keys"].update(msg.keys())
                    counters["roles"][str(msg.get("role"))] += 1
                if not messages or messages[-1].get("role") != "assistant":
                    flags.append("not_ending_assistant")
                valid_target = bool(messages and messages[-1].get("role") == "assistant")
                prompt = messages[:-1] if valid_target else messages
                target = messages[-1] if valid_target else {}
                users = [msg.get("content", "") for msg in prompt if msg.get("role") == "user"]
                last_user = users[-1] if users and isinstance(users[-1], str) else ""
                answer = target.get("content", "")
                reasoning = target.get("reasoning_content", "") or ""
                if not users:
                    flags.append("no_user_message")
                if not last_user.strip():
                    flags.append("empty_last_user")
                if not isinstance(answer, str) or not answer.strip():
                    flags.append("empty_or_invalid_final_answer")
                    answer = answer if isinstance(answer, str) else ""
                if any(not isinstance(msg.get("content"), str) for msg in messages):
                    flags.append("non_string_message_content")
                if any(msg.get("role") == "system" and not (msg.get("content") or "").strip() for msg in messages if isinstance(msg.get("content"), str)):
                    flags.append("empty_system_placeholder")
                if any(msg.get("role") in ["user", "assistant"] and isinstance(msg.get("content"), str) and not msg["content"].strip() for msg in prompt):
                    flags.append("empty_history_or_current_message")
                if len(answer.strip()) < 20:
                    flags.append("short_answer_under_20_chars_review_only")
                if answer.strip() and norm(answer) == norm(last_user):
                    flags.append("answer_copies_last_user_review_only")
                if len(QA_MARKERS.findall(last_user)) >= 6:
                    flags.append("embedded_multi_qa_review_only")
                if any(msg.get("role") == "tool" or msg.get("tool_calls") for msg in messages):
                    flags.append("tool_messages_or_calls")
                if len(NON_LATIN.findall(last_user[:4000])) >= 20:
                    flags.append("non_latin_script_in_user_review_only")
                if reasoning:
                    flags.append("final_assistant_has_separate_reasoning")
                counters["user_turns"][len(users)] += 1
                text = last_user[:4000]
                if len(text) < 80 and users and isinstance(users[0], str):
                    text += "\n" + users[0][:4000]
                signals = [name for name, pat in PATTERNS if pat.search(text)]
                hint = signals[0] if signals else "other_uncertain"
                counters["task_hints"][hint] += 1
                counters["task_signals"].update(signals)
                phash = fingerprint(prompt)
                prompts[phash] += 1
                first_answer = next((i for i, msg in enumerate(prompt) if msg.get("role") == "assistant"), len(prompt))
                seed_hash = fingerprint(prompt[:first_answer])
                seeds[seed_hash] += 1
                uuid = str(row.get("uuid") or "")
                if not uuid:
                    flags.append("missing_uuid")
                else:
                    uuids[uuid] += 1
                reference_hashes[hashlib.sha256(norm(answer).encode()).digest()] += 1
                if phash in existing:
                    flags.append("exact_prompt_overlap_existing_cleaned_data")
                hard = {"invalid_messages_structure", "not_ending_assistant", "no_user_message", "empty_last_user", "empty_or_invalid_final_answer", "non_string_message_content"}
                eligible = not hard.intersection(flags)
                strict_good += eligible
                counters["flags"].update(flags)
                plen = sum(len(msg.get("content", "")) for msg in prompt if isinstance(msg.get("content"), str))
                alen = len(answer)
                rlen = len(reasoning) if isinstance(reasoning, str) else 0
                pchars.append(plen); achars.append(alen); rchars.append(rlen)
                info = {"section": section, "row": idx, "byte_offset": position, "byte_length": len(line), "uuid": uuid,
                        "task_hint": hint, "task_signals": signals, "user_turns": len(users), "prompt_chars": plen,
                        "answer_chars": alen, "reasoning_chars": rlen, "structurally_valid": eligible,
                        "flags": flags, "prompt_sha256": phash.hex(), "conversation_seed_sha256": seed_hash.hex()}
                index_batch.append(info)
                if len(index_batch) >= 10000:
                    table = pa.Table.from_pylist(index_batch)
                    if writer is None:
                        writer = pq.ParquetWriter(OUT / "full_row_index.parquet", table.schema, compression="zstd")
                    writer.write_table(table.cast(writer.schema)); index_batch = []
                n = len(pchars)
                slot = n - 1 if n <= RESERVOIR_SIZE else rng.randrange(n)
                if slot < RESERVOIR_SIZE:
                    entry = {**info, "messages": messages}
                    if n <= RESERVOIR_SIZE:
                        reservoir.append(entry)
                    else:
                        reservoir[slot] = entry
                if (idx + 1) % 100000 == 0:
                    print(f"{section}: {idx + 1:,} rows, {offset / 1e9:.2f} GB scanned", flush=True)
        prompt_counts[section], seed_counts[section], uuid_counts[section] = prompts, seeds, uuids
        result["sections"][section] = {"rows": len(pchars), "invalid_json": bad_json,
                                      **{name: dict(c) for name, c in counters.items()},
                                      "structurally_valid_rows": strict_good, "prompt_chars": stats(pchars), "answer_chars": stats(achars),
                                      "reasoning_chars": stats(rchars), "unique_prompts": len(prompts), "unique_uuids": len(uuids),
                                      "duplicate_prompt_groups": sum(v > 1 for v in prompts.values()),
                                      "redundant_prompt_rows": sum(v - 1 for v in prompts.values()),
                                      "unique_conversation_seeds": len(seeds),
                                      "duplicate_conversation_seed_groups": sum(v > 1 for v in seeds.values()),
                                      "redundant_conversation_seed_rows": sum(v - 1 for v in seeds.values()),
                                      "duplicate_uuid_groups": sum(v > 1 for v in uuids.values()),
                                      "redundant_uuid_rows": sum(v - 1 for v in uuids.values()),
                                      "unique_reference_texts": len(reference_hashes)}
        reservoir_all.extend(reservoir)
        rng_review = random.Random(SEED)
        for r in rng_review.sample(reservoir, min(20, len(reservoir))):
            users = [x["content"] for x in r["messages"] if x.get("role") == "user"]
            answer = r["messages"][-1].get("content", "")
            diagnostic_examples.append({**{k: v for k, v in r.items() if k != "messages"},
                                        "last_user_excerpt": users[-1][:1800] if users else "", "answer_excerpt": answer[:2300]})
        (OUT / "full_audit_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(f"Finished {section}: {len(pchars):,} rows", flush=True)
    if index_batch:
        table = pa.Table.from_pylist(index_batch)
        if writer is None:
            writer = pq.ParquetWriter(OUT / "full_row_index.parquet", table.schema, compression="zstd")
        writer.write_table(table.cast(writer.schema))
    writer.close()
    cross_p = prompt_counts["reasoning_off"].keys() & prompt_counts["reasoning_on"].keys()
    cross_u = uuid_counts["reasoning_off"].keys() & uuid_counts["reasoning_on"].keys()
    cross_s = seed_counts["reasoning_off"].keys() & seed_counts["reasoning_on"].keys()
    result["between_sections"] = {"shared_prompt_keys": len(cross_p), "shared_uuid_keys": len(cross_u),
                                  "shared_conversation_seed_keys": len(cross_s),
                                  **{f"{s}_rows_with_shared_conversation_seed": sum(seed_counts[s][key] for key in cross_s) for s in seed_counts},
                                  **{f"{s}_rows_with_shared_prompt": sum(prompt_counts[s][key] for key in cross_p) for s in prompt_counts}}
    while not manifest_path.exists():
        time.sleep(5)
    result["download"] = json.loads(manifest_path.read_text())
    (OUT / "full_audit_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    with (OUT / "diagnostic_reservoir.jsonl").open("wb") as stream:
        for row in reservoir_all:
            stream.write(orjson.dumps(row) + b"\n")
    (OUT / "manual_review_excerpts.json").write_text(json.dumps(diagnostic_examples, ensure_ascii=False, indent=2) + "\n")
    print("Full scan complete; diagnostic reservoir saved. No final sample created.", flush=True)


if __name__ == "__main__":
    scan()
