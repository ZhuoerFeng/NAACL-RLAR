#!/usr/bin/env python3
"""Approved 680/320 sampling, with deterministic order and explicit review gate.

prepare: build/cache candidates, provisional selection and complete review queue.
finalize: export only after all flags and 100 random full-conversation reviews pass.
No existing train/valid files are modified.
"""
import os
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("RAYON_NUM_THREADS", "4")
from collections import Counter
from datetime import datetime, timezone
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re

import numpy as np
import orjson
import pyarrow as pa
import pyarrow.parquet as pq
from langid.langid import LanguageIdentifier, model
from sklearn.feature_extraction.text import TfidfVectorizer
from transformers import AutoTokenizer

from audit_nemotron import fingerprint, norm, stats

BASE = Path(__file__).resolve().parents[1]
RAW = BASE / "data/raw/nemotron-instruction-following-chat-v2"
AUDIT = BASE / "analysis/results/nemotron_sampling_review"
WORK = BASE / "analysis/results/nemotron_sample_1000"
OUT = BASE / "data/nemotron_english_off_1000"
SEED = 20260923
QUOTAS = {1: 680, 2: 320}
REVISION = "1a9454ed054b8544503ab8d8c0a519d141a44c5b"


def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def priority(domain, key):
    return hashlib.sha256(f"{SEED}:{domain}:{key}".encode()).hexdigest()


def save_json(path, value):
    path.write_bytes(orjson.dumps(value, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS) + b"\n")


def write_jsonl(path, rows):
    with path.open("wb") as f:
        for row in rows:
            f.write(orjson.dumps(row) + b"\n")


def load_jsonl(path):
    return [orjson.loads(line) for line in path.open("rb")] if path.exists() else []


def build_order():
    path = WORK / "ordered_groups.parquet"
    if path.exists():
        return pq.read_table(path)
    cols = ["row", "uuid", "byte_offset", "byte_length", "user_turns", "prompt_sha256", "conversation_seed_sha256", "task_hint", "flags", "structurally_valid"]
    table = pq.read_table(AUDIT / "full_row_index.parquet", columns=cols, filters=[("section", "=", "reasoning_off")])
    df = table.to_pandas()
    df["member_priority"] = [priority("member", u) for u in df.uuid]
    # Choose one original record uniformly (via pseudorandom hash order) per
    # conversation seed BEFORE stratum assignment. This also prevents cross-stratum duplicates.
    df = df.sort_values("member_priority", kind="stable").drop_duplicates("conversation_seed_sha256", keep="first")
    df["random_priority"] = [priority("group", h) for h in df.conversation_seed_sha256]
    df = df.sort_values("random_priority", kind="stable")
    df["random_rank"] = df.groupby("user_turns").cumcount()
    table = pa.Table.from_pandas(df, preserve_index=False)
    pq.write_table(table, path, compression="zstd")
    save_json(WORK / "grouping_summary.json", {"original_rows": 1068273, "unique_seed_groups": len(df),
        "representative_user_turns": dict(Counter(int(v) for v in df.user_turns)),
        "method": "SHA256(seed:member:uuid) chooses one representative per normalized initial conversation input; SHA256(seed:group:seed_hash) provides random group order; original representatives then stratified by user turns."})
    return table


def language_text(text):
    # Keep all source text unchanged; masks only affect language identification.
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`\n]+`", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\\\[.*?\\\]|\$\$.*?\$\$", " ", text, flags=re.S)
    return text.strip()


def neutral(text):
    text = text.strip()
    return bool(re.fullmatch(r"[\d\W_]+", text) or re.fullmatch(r"[A-Ea-e]", text) or re.fullmatch(r"(?:####\s*)?[-+\d.,%$ /\\{}()=]+", text))


def chemical_claim_review(original):
    """Targeted expansion after repeated unsupported chemical profiles/articles.

    Only direct first-user requests are flagged; quoted historical Q/A is excluded.
    This is a review trigger, never a category-level rejection.
    """
    first = next(m["content"] for m in original["messages"] if m["role"] == "user").strip()
    return bool(re.match(r"(?:Give me an introduction|Write (?:an? |a \d+ word )?(?:article|introduction))\b", first, re.I)
                and re.search(r"chemical (?:company|industry)", first, re.I))


def inspect(row, original, lid):
    messages = original["messages"]
    reasons, flags, languages = [], [], []
    if original.get("reasoning") != "off": reasons.append("wrong_reasoning_mode")
    sequence = [m["role"] for m in messages if m["role"] != "system"]
    if sequence != ["user", "assistant"] * int(row["user_turns"]): reasons.append("invalid_role_sequence")
    if not row["structurally_valid"]: reasons.append("invalid_structure")
    for i, msg in enumerate(messages):
        content = msg.get("content", "")
        if not isinstance(content, str): reasons.append("non_string_content"); continue
        if msg["role"] == "system" and not content.strip(): continue
        if not content.strip(): reasons.append("empty_user_or_assistant"); continue
        text = language_text(content)
        if not text or neutral(text):
            languages.append({"message_index": i, "role": msg["role"], "hint": "neutral", "confidence": None})
            continue
        # Inspect beginning and end of long messages to catch mixed-language targets.
        probes = [text[:4000]] + ([text[-4000:]] if len(text) > 6000 else [])
        for segment, probe in enumerate(probes):
            hint, score = lid.classify(probe)
            if hint != "en":
                folded_hint, folded_score = lid.classify(probe.casefold())
                if folded_hint == "en" and folded_score >= .95:
                    hint, score = folded_hint, folded_score
            languages.append({"message_index": i, "role": msg["role"], "segment": segment, "hint": hint, "confidence": score})
            if hint != "en" or score < .90:
                code_like = bool(re.search(r"```|\b(?:translate|translation|code|json|sql)\b|(?m:^\s*(?:def |class |import |public |#include|function |\{|\[))|;\s*$", content, re.I))
                if hint != "en" and score >= .99 and len(probe) >= 100 and not code_like:
                    reasons.append("confident_non_english_message")
                else:
                    flags.append("language_needs_review")
    for flag in row["flags"]:
        if flag in {"short_answer_under_20_chars_review_only", "answer_copies_last_user_review_only", "embedded_multi_qa_review_only", "non_latin_script_in_user_review_only"}:
            flags.append(flag)
    if chemical_claim_review(original): flags.append("chemical_factual_claims_needs_review")
    users = [m["content"] for m in messages if m["role"] == "user"]
    final_user, answer = users[-1], messages[-1]["content"]
    if re.search(r"\b(?:I (?:cannot|can't|won't|am unable to)|I'm (?:sorry|unable)|I’m (?:sorry|unable))\b", answer[:500], re.I):
        flags.append("refusal_or_inability_needs_review")
    qas = list(re.finditer(r"(?:^|\n)\s*(?:q|question)\s*:", final_user, re.I))
    active = final_user[qas[-1].end():] if qas else final_user
    word_constraints = []
    for match in re.finditer(r"(?P<prefix>(?:at least|at most|no more than|less than|under|exactly|around|about|approximately)\s+)?(?P<n>\d[\d,]*)[\s\-‑–]*(?:words?|word\s+(?:essay|article|summary))\b", active, re.I):
        n = int(match["n"].replace(",", "")); observed = len(answer.split())
        if n < 30 or n > 50000: continue
        prefix = (match["prefix"] or "").strip().lower()
        bad = observed < n if prefix == "at least" else observed > n if prefix in {"at most", "no more than", "less than", "under"} else abs(observed-n) > max(3, n*(.05 if prefix == "exactly" else .20))
        word_constraints.append({"request": match.group(), "target": n, "observed_whitespace_words": observed, "potential_violation": bad})
        if bad: flags.append("explicit_word_count_needs_review")
    if re.search(r"\bexactly\s+(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:sentences?|paragraphs?|bullet|lines?)", active, re.I):
        flags.append("explicit_format_needs_review")
    if answer.count("```") % 2: flags.append("unbalanced_code_fence_needs_review")
    if re.search(r"\b(?:in (?:this|the) (?:image|picture|screenshot|video)|attached (?:image|file)|image attached)\b|<image>", active, re.I):
        flags.append("external_context_needs_review")
    return {"auto_reject_reasons": sorted(set(reasons)), "review_flags": sorted(set(flags)), "language_checks": languages, "word_constraints": word_constraints}


def existing_data():
    prompts, seed_hashes, last_users, locs = set(), set(), [], []
    for split in ["train", "valid"]:
        rows = pq.read_table(BASE / "data/cleaned" / f"{split}.parquet", columns=["prompt", "data_source"]).to_pylist()
        for i, r in enumerate(rows):
            prompt = r["prompt"]
            prompts.add(fingerprint(prompt).hex())
            stop = next((j for j,m in enumerate(prompt) if m["role"] == "assistant"), len(prompt))
            seed_hashes.add(fingerprint(prompt[:stop]).hex())
            users = [m["content"] for m in prompt if m["role"] == "user"]
            text = users[-1] if users else ""
            last_users.append(text[:4000] + ("\n" + text[-4000:] if len(text)>4000 else ""))
            locs.append({"split":split,"row":i,"data_source":r["data_source"]})
    return prompts, seed_hashes, last_users, locs


def prepare():
    WORK.mkdir(parents=True, exist_ok=True)
    order = build_order()
    old = {r["uuid"]: r for r in load_jsonl(WORK / "candidate_cache.jsonl")}
    prompts, seeds, existing_texts, locs = existing_data()
    lid = LanguageIdentifier.from_modelstring(model, norm_probs=True)
    with (RAW / "data/reasoning_off.jsonl").open("rb") as stream:
        for row in order.to_pylist():
            limit = {1:1100, 2:600}.get(row["user_turns"],0)
            if row["random_rank"] >= limit or row["uuid"] in old: continue
            stream.seek(row["byte_offset"])
            original = orjson.loads(stream.read(row["byte_length"]))
            assert original["uuid"] == row["uuid"]
            info = inspect(row, original, lid)
            if row["prompt_sha256"] in prompts: info["auto_reject_reasons"].append("exact_existing_prompt")
            if row["conversation_seed_sha256"] in seeds: info["auto_reject_reasons"].append("exact_existing_conversation_seed")
            old[row["uuid"]] = {**row, **info, "original": original}
    cache = sorted(old.values(),key=lambda r:(r["user_turns"],r["random_rank"]))
    for r in cache:
        if chemical_claim_review(r["original"]):
            r["review_flags"] = sorted(set(r["review_flags"]) | {"chemical_factual_claims_needs_review"})
    if not (WORK / "near_duplicate_scan.json").exists():
        vec = TfidfVectorizer(ngram_range=(1,2), max_features=100000, dtype=np.float32)
        existing_matrix = vec.fit_transform(existing_texts)
        for begin in range(0,len(cache),64):
            batch=cache[begin:begin+64]
            texts=[([m["content"] for m in r["original"]["messages"] if m["role"]=="user"][-1]) for r in batch]
            probes=[t[:4000]+("\n"+t[-4000:] if len(t)>4000 else "") for t in texts]
            sim=(vec.transform(probes) @ existing_matrix.T).toarray()
            for i,r in enumerate(batch):
                j=int(sim[i].argmax());score=float(sim[i,j])
                r["nearest_existing"]={**locs[j],"cosine":score}
                if score>=.85 and len(texts[i])>=120:
                    r["review_flags"].append("near_existing_prompt_needs_review")
        save_json(WORK / "near_duplicate_scan.json", {"method":"TF-IDF word 1/2-grams fitted on existing cleaned last-user inputs; first/last 4000 characters; max_features 100000; flag cosine >=0.85 and candidate >=120 chars. Approximate screening, not exhaustive semantic deduplication.","existing_rows":len(locs),"candidate_rows":len(cache)})
    write_jsonl(WORK / "candidate_cache.jsonl",cache)
    decisions = {r["uuid"]:r for r in load_jsonl(WORK / "review_decisions.jsonl")}
    selected, rejects, seen_prompt, seen_seed = [], [], set(), set()
    counts = Counter()
    for r in cache:
        turns=r["user_turns"]
        if counts[turns]>=QUOTAS[turns]: continue
        reasons=list(r["auto_reject_reasons"])
        decision=decisions.get(r["uuid"],{})
        if decision.get("decision")=="reject": reasons.append("review_reject:"+decision["reason"])
        if r["prompt_sha256"] in seen_prompt or r["conversation_seed_sha256"] in seen_seed: reasons.append("duplicate_selected_question")
        if reasons:
            rejects.append({"uuid":r["uuid"],"row":r["row"],"user_turns":turns,"random_rank":r["random_rank"],"reasons":reasons});continue
        selected.append(r);counts[turns]+=1;seen_prompt.add(r["prompt_sha256"]);seen_seed.add(r["conversation_seed_sha256"])
    assert counts==Counter(QUOTAS), f"Not enough candidates: {counts}; extend candidate limits explicitly"
    sample_ids={r["uuid"] for r in sorted(selected,key=lambda r:priority("semantic_review",r["uuid"]))[:100]}
    queue=[]
    for r in selected:
        triggers=list(r["review_flags"])+(["random_100_full_conversation_review"] if r["uuid"] in sample_ids else [])
        if triggers:
            done=decisions.get(r["uuid"],{})
            needed = not (done.get("decision")=="accept" and ("random_100_full_conversation_review" not in triggers or done.get("full_conversation_reviewed") is True))
            if needed: queue.append({**r,"review_triggers":triggers})
    write_jsonl(WORK / "provisional_selection.jsonl",selected)
    write_jsonl(WORK / "rejection_log.jsonl",rejects)
    write_jsonl(WORK / "pending_review.jsonl",queue)
    save_json(WORK / "random_100_ids.json",sorted(sample_ids))
    save_json(WORK / "progress.json",{"selected":dict(counts),"cache_rows":len(cache),"visited_rejects":len(rejects),"pending_reviews":len(queue),"pending_random_full_reviews":sum("random_100_full_conversation_review" in r["review_triggers"] for r in queue),"review_trigger_counts":dict(Counter(f for r in queue for f in r["review_triggers"]))})
    print((WORK / "progress.json").read_text())
    return selected, queue


def finalize():
    selected, queue = prepare()
    assert not queue, "Complete all required reviews before final export"
    OUT.mkdir(parents=True,exist_ok=True)
    assert not (OUT / "sample.jsonl").exists(), "Final output already exists; do not silently overwrite"
    # Final ordering is independently randomized, so records are not grouped by turn count.
    selected.sort(key=lambda r:priority("output",r["uuid"]))
    tok = AutoTokenizer.from_pretrained(str(BASE / "model_tokeniser"),local_files_only=True)
    metadata=[]
    decisions={r["uuid"]:r for r in load_jsonl(WORK / "review_decisions.jsonl")}
    random_ids=set(orjson.loads((WORK / "random_100_ids.json").read_bytes()))
    for begin in range(0,len(selected),64):
        batch=selected[begin:begin+64]
        prompts=[r["original"]["messages"][:-1] for r in batch]
        on=tok.apply_chat_template(prompts,tokenize=True,add_generation_prompt=True,enable_thinking=True,truncation=False,padding=False)
        off=tok.apply_chat_template(prompts,tokenize=True,add_generation_prompt=True,enable_thinking=False,truncation=False,padding=False)
        answer=tok([r["original"]["messages"][-1]["content"] for r in batch],add_special_tokens=False,truncation=False,padding=False)["input_ids"]
        for j,r in enumerate(batch):
            metadata.append({"sample_index":begin+j,**{k:r[k] for k in ["uuid","row","byte_offset","byte_length","user_turns","prompt_sha256","conversation_seed_sha256","random_priority","random_rank","task_hint","language_checks","review_flags","nearest_existing"]},"source_repo":"nvidia/Nemotron-SFT-Instruction-Following-Chat-v2","revision":REVISION,"source_file":"data/reasoning_off.jsonl","prompt_tokens_thinking":len(on[j]),"prompt_tokens_nonthinking":len(off[j]),"answer_tokens":len(answer[j]),"over_10000_prompt_tokens":max(len(on[j]),len(off[j]))>10000,"random_100_review":r["uuid"] in random_ids,"review_decision":decisions.get(r["uuid"])})
    originals=[r["original"] for r in selected]
    write_jsonl(OUT / "sample.jsonl",originals)
    pq.write_table(pa.Table.from_pylist(originals),OUT / "sample.parquet",compression="zstd")
    write_jsonl(OUT / "provenance.jsonl",metadata)
    pq.write_table(pa.Table.from_pylist(metadata),OUT / "provenance.parquet",compression="zstd")
    write_jsonl(OUT / "overlong_prompts.jsonl",[r for r in metadata if r["over_10000_prompt_tokens"]])
    for name in ["rejection_log.jsonl","review_decisions.jsonl","random_100_ids.json","grouping_summary.json","near_duplicate_scan.json"]:
        (OUT/name).write_bytes((WORK/name).read_bytes())
    baseline=orjson.loads((BASE / "data/cleaned/manifest.json").read_bytes())
    unchanged={name:digest(BASE / "data/cleaned" / name)==info["sha256"] for name,info in baseline["files"].items()}
    assert all(unchanged.values())
    manifest={"created_utc":datetime.now(timezone.utc).isoformat(),"user_approved_plan":"English reasoning_off, 680 single-turn / 320 two-turn; approved in task", "rows":len(originals),"seed":SEED,"user_turn_counts":dict(Counter(r["user_turns"] for r in metadata)),"source_download_manifest":orjson.loads((RAW / "download_manifest.json").read_bytes()),"grouping":orjson.loads((WORK / "grouping_summary.json").read_bytes()),"rejection_reason_counts":dict(Counter(reason for r in load_jsonl(WORK / "rejection_log.jsonl") for reason in r["reasons"])),"task_hints":dict(Counter(r["task_hint"] for r in metadata)),"tokenizer_path":str(BASE / "model_tokeniser"),"tokenizer_class":type(tok).__name__,"tokenizer_config_sha256":digest(BASE / "model_tokeniser/tokenizer_config.json"),"versions":{p:importlib.metadata.version(p) for p in ["transformers","tokenizers","langid","numpy","pyarrow","scikit-learn"]},"prompt_token_method":"Full original history excluding final assistant; Qwen chat template with generation prompt, thinking true/false; no truncation.","token_stats":{k:stats([r[k] for r in metadata]) for k in ["prompt_tokens_thinking","prompt_tokens_nonthinking","answer_tokens"]},"over_10000_count":sum(r["over_10000_prompt_tokens"] for r in metadata),"quality_review":{"final_random_full_reviews":sum(r["random_100_review"] for r in metadata),"selected_records_with_review":sum(r["review_decision"] is not None for r in metadata),"scope":"Structure and exact dedup for all records; 100 random full-conversation semantic reviews plus all flagged candidates. Not exhaustive factual verification."},"existing_cleaned_data_sha256_unchanged":unchanged,"original_messages_unmodified":True,"no_train_valid_merge":True}
    manifest["files"]={p.name:{"bytes":p.stat().st_size,"sha256":digest(p)} for p in sorted(OUT.iterdir()) if p.is_file()}
    manifest["overlong_definition"] = "Either Qwen thinking or nonthinking full prompt exceeds 10000 tokens; retained without truncation."
    manifest["tokenizer_files_sha256"] = {p.name:digest(p) for p in sorted((BASE / "model_tokeniser").iterdir()) if p.is_file()}
    manifest["reproduction_files_sha256"] = {str(p.relative_to(BASE)):digest(p) for p in [Path(__file__), BASE / "analysis/audit_nemotron.py", WORK / "review_decisions.jsonl", WORK / "ordered_groups.parquet"]}
    save_json(OUT / "manifest.json",manifest)
    print(f"Exported {len(originals)} records to {OUT}")


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=["prepare","finalize"])
    args=parser.parse_args()
    prepare() if args.action=="prepare" else finalize()
