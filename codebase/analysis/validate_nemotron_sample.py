"""Validate the independent Nemotron export against raw records and clean data.

This reads dataset text as data; no embedded code is executed.
"""
import hashlib
import json
import os
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("RAYON_NUM_THREADS", "4")
import orjson
import pyarrow.parquet as pq
from transformers import AutoTokenizer

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "data/nemotron_english_off_1000"


def read_lines(path):
    return [json.loads(line) for line in path.open()]


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def fingerprint(messages):
    content = [[m["role"], " ".join(unicodedata.normalize("NFC", m["content"]).split())]
               for m in messages if not (m["role"] == "system" and not m["content"].strip())]
    return hashlib.sha256(orjson.dumps(content)).hexdigest()


def main():
    rows = read_lines(OUT / "sample.jsonl")
    provenance = read_lines(OUT / "provenance.jsonl")
    manifest = json.loads((OUT / "manifest.json").read_text())
    assert len(rows) == len(provenance) == 1000
    assert rows == pq.read_table(OUT / "sample.parquet").to_pylist()
    # Arrow makes absent nested nullable fields explicit. Compare via its schema.
    import pyarrow as pa
    pt = pq.read_table(OUT / "provenance.parquet")
    assert pt.equals(pa.Table.from_pylist(provenance, schema=pt.schema))
    for name, info in manifest["files"].items():
        assert sha(OUT / name) == info["sha256"], name
    for name, expected in manifest["tokenizer_files_sha256"].items():
        assert sha(BASE / "model_tokeniser" / name) == expected
    for name, expected in manifest["reproduction_files_sha256"].items():
        assert sha(BASE / name) == expected

    random_ids = set(json.loads((OUT / "random_100_ids.json").read_text()))
    expected_random = {r["uuid"] for r in sorted(rows, key=lambda r: hashlib.sha256(
        f'20260923:semantic_review:{r["uuid"]}'.encode()).hexdigest())[:100]}
    assert len(random_ids) == 100 and random_ids == expected_random
    uuids, prompts, seeds = set(), set(), set()
    turns = Counter()
    with (BASE / "data/raw/nemotron-instruction-following-chat-v2/data/reasoning_off.jsonl").open("rb") as raw:
        for i, (r, p) in enumerate(zip(rows, provenance)):
            assert r["reasoning"] == "off" and p["sample_index"] == i
            assert r["uuid"] == p["uuid"]
            raw.seek(p["byte_offset"])
            assert json.loads(raw.read(p["byte_length"])) == r, (i, "raw mismatch")
            messages = r["messages"]
            roles = [m["role"] for m in messages if m["role"] != "system"]
            n = roles.count("user")
            assert n == p["user_turns"] and roles == ["user", "assistant"] * n
            assert messages[-1]["role"] == "assistant"
            assert all(isinstance(m["content"], str) and (m["content"].strip() or m["role"] == "system") for m in messages)
            stop = next(j for j, m in enumerate(messages) if m["role"] == "assistant")
            ph, sh = fingerprint(messages[:-1]), fingerprint(messages[:stop])
            assert ph == p["prompt_sha256"] and sh == p["conversation_seed_sha256"]
            uuids.add(r["uuid"]); prompts.add(ph); seeds.add(sh); turns[n] += 1
            if p["review_flags"] or r["uuid"] in random_ids:
                assert p["review_decision"]["decision"] == "accept"
            assert p["random_100_review"] == (r["uuid"] in random_ids)
            if r["uuid"] in random_ids:
                assert p["review_decision"]["full_conversation_reviewed"] is True
    assert len(uuids) == len(prompts) == len(seeds) == 1000
    assert turns == {1: 680, 2: 320}
    rejected = {r["uuid"] for r in read_lines(OUT / "rejection_log.jsonl")}
    assert not uuids & rejected

    unchanged = {}
    expected = {"train": "8f46b61607e195e4b84f23c079933775b329f8924868d11724712a488e326b33",
                "valid": "0fa365222cd9f6426a4a32beb11f5c5d9e8839e9538683db5dfc5ebc2d4a0b9b"}
    existing_prompts, existing_seeds = set(), set()
    for split in ["train", "valid"]:
        for location in ["data/cleaned", "data"]:
            path = BASE / location / f"{split}.parquet"
            unchanged[str(path.relative_to(BASE))] = sha(path) == expected[split]
        clean = pq.read_table(BASE / "data/cleaned" / f"{split}.parquet", columns=["prompt"]).to_pylist()
        for item in clean:
            prompt = item["prompt"]
            stop = next((j for j, m in enumerate(prompt) if m["role"] == "assistant"), len(prompt))
            existing_prompts.add(fingerprint(prompt)); existing_seeds.add(fingerprint(prompt[:stop]))
    assert all(unchanged.values())
    assert not (prompts & existing_prompts or seeds & existing_seeds)

    tokenizer = AutoTokenizer.from_pretrained(BASE / "model_tokeniser", local_files_only=True)
    # Independent path: render chat template to text, then encode without special tokens.
    for thinking, key in [(False, "prompt_tokens_nonthinking"), (True, "prompt_tokens_thinking")]:
        for begin in range(0, 1000, 64):
            texts = [tokenizer.apply_chat_template(r["messages"][:-1], tokenize=False,
                     add_generation_prompt=True, enable_thinking=thinking) for r in rows[begin:begin+64]]
            encoded = tokenizer(texts, add_special_tokens=False, truncation=False)["input_ids"]
            assert [len(v) for v in encoded] == [p[key] for p in provenance[begin:begin+64]]
    answers = tokenizer([r["messages"][-1]["content"] for r in rows], add_special_tokens=False, truncation=False)["input_ids"]
    assert [len(v) for v in answers] == [p["answer_tokens"] for p in provenance]
    overlong = [p for p in provenance if max(p["prompt_tokens_thinking"], p["prompt_tokens_nonthinking"]) > 10000]
    assert overlong == read_lines(OUT / "overlong_prompts.jsonl")
    assert all(p["over_10000_prompt_tokens"] == (p in overlong) for p in provenance)

    report = {"validated_utc": datetime.now(timezone.utc).isoformat(), "status": "passed", "rows": 1000,
              "turns": dict(turns), "raw_original_matches": 1000, "jsonl_parquet_equal": True,
              "unique_uuids": len(uuids), "unique_normalized_prompts": len(prompts), "unique_initial_inputs": len(seeds),
              "existing_exact_prompt_and_seed_overlap": 0, "random_full_reviews": 100,
              "all_selected_flags_resolved": True, "qwen_lengths_independently_recomputed": 1000,
              "over_10000_prompts": len(overlong), "train_valid_sha256_unchanged": unchanged,
              "script_sha256": sha(Path(__file__)),
              "limitation": "Integrity/reproducibility checks, not an exhaustive factual or safety evaluation of answers."}
    # Revalidation should not invalidate the package manifest merely by changing
    # a timestamp. Preserve an equivalent prior report byte-for-byte.
    report_path = OUT / "validation_report.json"
    if report_path.exists():
        previous = json.loads(report_path.read_text())
        comparable = lambda value: {k:v for k,v in value.items() if k != "validated_utc"}
        if comparable(previous) == comparable(report):
            report = previous
        else:
            report_path.write_text(json.dumps(report, indent=2) + "\n")
    else:
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
