#!/usr/bin/env python3
"""Qwen token and language estimates on the full-scan random diagnostic reservoir.

This does not select or write the user's final 1,000-record sample.
"""
import os
os.environ.setdefault("RAYON_NUM_THREADS", "4")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
from collections import Counter
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import re
import time

import numpy as np
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from transformers import AutoTokenizer
from langid.langid import LanguageIdentifier, model

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / "analysis/results/nemotron_sampling_review"

def describe(values):
    a = np.asarray(values)
    return {"n": len(a), "mean": float(a.mean()), **{k: float(np.quantile(a, q)) for k, q in [("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99), ("max", 1)]}}

def bins(values):
    bounds = [0, 512, 2048, 4096, 10000, float("inf")]
    labels = ["<=512", "513-2048", "2049-4096", "4097-10000", ">10000"]
    return {label: sum(lo < v <= hi for v in values) for label, lo, hi in zip(labels, bounds, bounds[1:])}

def main():
    reservoir_path = OUT / "diagnostic_reservoir.jsonl"
    while not reservoir_path.exists():
        time.sleep(5)
    rows = [orjson.loads(line) for line in reservoir_path.open("rb")]
    assert len(rows) == 16384, "Full-scan diagnostic reservoir incomplete"
    tok = AutoTokenizer.from_pretrained(str(BASE / "model_tokeniser"), local_files_only=True)
    lid = LanguageIdentifier.from_modelstring(model, norm_probs=True)
    lengths = []
    for start in range(0, len(rows), 64):
        chunk = rows[start:start+64]
        prompts = [r["messages"][:-1] for r in chunk]
        on = tok.apply_chat_template(prompts, tokenize=True, add_generation_prompt=True, enable_thinking=True, truncation=False, padding=False)
        off = tok.apply_chat_template(prompts, tokenize=True, add_generation_prompt=True, enable_thinking=False, truncation=False, padding=False)
        answers = tok([r["messages"][-1].get("content", "") for r in chunk], add_special_tokens=False, truncation=False, padding=False)["input_ids"]
        reasoning = tok([r["messages"][-1].get("reasoning_content", "") or "" for r in chunk], add_special_tokens=False, truncation=False, padding=False)["input_ids"]
        for j, r in enumerate(chunk):
            user = [m["content"] for m in r["messages"] if m.get("role") == "user"][-1]
            # Language hints are estimated, especially uncertain for short/code text.
            lang, confidence = lid.classify(user[:4000])
            answer = r["messages"][-1].get("content", "")
            answer_lang, answer_confidence = lid.classify(answer[:4000])
            refusal = bool(re.search(r"\b(?:I (?:cannot|can't|won't|am unable to)|I'm (?:sorry|unable)|I’m (?:sorry|unable))\b", answer[:500], re.I))
            lengths.append({**{k: v for k, v in r.items() if k != "messages"}, "prompt_tokens_thinking": len(on[j]),
                            "prompt_tokens_nonthinking": len(off[j]), "answer_tokens": len(answers[j]),
                            "reasoning_tokens": len(reasoning[j]), "language_hint": lang,
                            "language_model_confidence": confidence, "short_user_for_language_id": len(user.strip()) < 50,
                            "answer_language_hint": answer_lang, "answer_language_model_confidence": answer_confidence,
                            "english_refusal_phrase_review_only": refusal})
        if start % 1024 == 0:
            print(f"Profiled {min(start+64, len(rows)):,}/{len(rows):,} diagnostic rows", flush=True)
    pq.write_table(pa.Table.from_pylist(lengths), OUT / "diagnostic_token_lengths.parquet", compression="zstd")
    result = {"method": "Seeded algorithm-R reservoir of 8192 records per official section. All numbers below are diagnostic sample estimates, not full-population token/language measurements.",
              "seed": 20260923, "tokenizer": str(BASE / "model_tokeniser"), "tokenizer_class": type(tok).__name__,
              "tokenizer_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (BASE / "model_tokeniser").glob("*.json")},
              "versions": {p: importlib.metadata.version(p) for p in ["transformers", "tokenizers", "langid"]},
              "prompt_method": "Full history excluding final assistant, Qwen apply_chat_template with generation prompt; enable_thinking true and false; no truncation. Separate final reasoning_content is never added to the input.",
              "language_method": "langid on first 4000 characters of last user; heuristic, not validated language labels; short prompts and code can be misclassified.",
              "sections": {}}
    for section in ["reasoning_off", "reasoning_on"]:
        part = [r for r in lengths if r["section"] == section]
        result["sections"][section] = {"n": len(part),
            **{field: describe([r[field] for r in part]) for field in ["prompt_tokens_thinking", "prompt_tokens_nonthinking", "answer_tokens", "reasoning_tokens"]},
            "prompt_token_bins_thinking": bins([r["prompt_tokens_thinking"] for r in part]),
            "nonthinking_minus_thinking": dict(Counter(r["prompt_tokens_nonthinking"]-r["prompt_tokens_thinking"] for r in part)),
            "language_hints": dict(Counter(r["language_hint"] for r in part).most_common()),
            "answer_language_hints": dict(Counter(r["answer_language_hint"] for r in part).most_common()),
            "short_user_for_language_id": sum(r["short_user_for_language_id"] for r in part),
            "english_refusal_phrase_review_only": sum(r["english_refusal_phrase_review_only"] for r in part)}
    (OUT / "diagnostic_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n")
    # Reproducible, deliberately diverse excerpts for human/model review.
    review = []
    for section in ["reasoning_off", "reasoning_on"]:
        rng = random.Random(20260924)
        for hint in sorted({r["task_hint"] for r in rows}):
            group = [r for r in rows if r["section"] == section and r["task_hint"] == hint]
            for r in rng.sample(group, min(2, len(group))):
                users = [m["content"] for m in r["messages"] if m.get("role") == "user"]
                answer = r["messages"][-1].get("content", "")
                review.append({k: r[k] for k in ["section", "row", "uuid", "task_hint", "user_turns"]} | {
                    "last_user": users[-1][:2500], "answer": answer[:3500],
                    "user_truncated": len(users[-1]) > 2500, "answer_truncated": len(answer) > 3500})
    (OUT / "stratified_review_excerpts.json").write_text(json.dumps(review, ensure_ascii=False, indent=2)+"\n")
    print("Diagnostic token and language profiling complete. No final sample produced.", flush=True)

if __name__ == "__main__":
    main()
