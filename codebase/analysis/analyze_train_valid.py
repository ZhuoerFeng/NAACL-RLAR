#!/usr/bin/env python3
"""Read-only audit of the local RLAR parquet files; row IDs are zero-based.

Run: python codebase/analysis/analyze_train_valid.py
Dependencies: pyarrow, numpy, scipy, scikit-learn, matplotlib.
No dataset text is sent to external services. Lengths are Unicode characters,
not model tokens; near-duplicate scores are screening signals, not judgments.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import random
import unicodedata

import numpy as np
import pyarrow.parquet as pq


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def norm(text):
    return " ".join(unicodedata.normalize("NFC", text).split())


def summary(values):
    values = np.asarray(values, dtype=float)
    if len(values) == 0:
        return {}
    result = {k: round(float(np.quantile(values, q)), 2) for k, q in
              [("min", 0), ("p50", .5), ("p90", .9), ("p95", .95), ("p99", .99), ("max", 1)]}
    return {"n": len(values), "mean": round(float(values.mean()), 2), **result}


def body(row):
    """Remove only observed task boilerplate for lexical overlap screening."""
    q = row["extra_info"]["question"] or ""
    src = row["data_source"]
    if src.startswith("english-french-translation"):
        m = re.search(r"Start your translation task now\. \[(?:English|French) Input\]\n(.*)\n\[(?:French|English) Output\]", q, re.S)
        return m.group(1) if m else q
    if src.endswith("-summarization"):
        m = re.search(r"Start your summarization task now\. \[(?:Article|Report) Input\]\n(.*)\n\[(?:Abstract|Summary) Output\]", q, re.S)
        return m.group(1) if m else q
    if src.startswith("essay_"):
        return q.split("\n\n", 1)[-1]
    return "\n".join(m["role"] + ": " + (m["content"] or "") for m in row["prompt"])


def location(row):
    return {"split": row["_split"], "row": row["_row"], "source": row["data_source"]}


def group_audit(rows, key):
    groups = defaultdict(list)
    for row in rows:
        value = key(row)
        if value is not None:
            groups[digest(value)].append(row)
    stats = {}
    for split in ["train", "valid"]:
        subsets = [[r for r in g if r["_split"] == split] for g in groups.values()]
        dup = [g for g in subsets if len(g) > 1]
        stats[split] = {"duplicate_groups": len(dup), "rows_in_duplicate_groups": sum(map(len, dup)),
                        "redundant_rows": sum(len(g) - 1 for g in dup)}
    cross = [g for g in groups.values() if len({r["_split"] for r in g}) > 1]
    stats["cross"] = {"shared_keys": len(cross), **{s + "_rows": sum(r["_split"] == s for g in cross for r in g) for s in ["train", "valid"]}}
    evidence = [[location(r) for r in g] for g in groups.values() if len(g) > 1]
    return stats, evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parents[1] / "data")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent / "results")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_rows, metrics, evidence = [], {"created_utc": datetime.now(timezone.utc).isoformat(), "files": {}}, {}
    schemas = []
    for split in ["train", "valid"]:
        path = args.data_dir / f"{split}.parquet"
        table = pq.read_table(path)
        rows = table.to_pylist()
        schemas.append(table.schema)
        metrics["files"][split] = {"rows": len(rows), "bytes": path.stat().st_size,
                                  "sha256": hashlib.file_digest(path.open("rb"), "sha256").hexdigest(),
                                  "schema": str(table.schema), "top_level_nulls": {k: table[k].null_count for k in table.column_names}}
        for i, row in enumerate(rows):
            row.update(_split=split, _row=i)
            row["_body"] = body(row)
            row["_prompt_chars"] = sum(len(m["content"] or "") for m in row["prompt"])
            row["_answer_chars"] = len(row["reward_model"]["ground_truth"] or "")
        all_rows.extend(rows)
        print(f"Loaded {split}: {len(rows):,} rows", flush=True)
    metrics["schemas_equal"] = schemas[0].equals(schemas[1], check_metadata=False)
    sources = sorted({r["data_source"] for r in all_rows})
    metrics["by_source"], metrics["by_split"], evidence["quality_flags"] = {}, {}, []
    for split in ["train", "valid"]:
        rows = [r for r in all_rows if r["_split"] == split]
        split_metrics = metrics["by_split"][split] = {
            "sources": dict(Counter(r["data_source"] for r in rows)),
            "abilities": dict(Counter(r["ability"] for r in rows)),
            "styles": dict(Counter(r["reward_model"]["style"] for r in rows)),
            "functions": dict(Counter(r["extra_info"]["function_call"] for r in rows)),
            "metadata_split": dict(Counter(r["extra_info"]["split"] for r in rows)),
            "prompt_chars": summary([r["_prompt_chars"] for r in rows]),
            "answer_chars": summary([r["_answer_chars"] for r in rows]),
            "message_count": summary([len(r["prompt"]) for r in rows]),
            "roles": dict(Counter(m["role"] for r in rows for m in r["prompt"])),
            "answer_equals_ground_truth": sum(r["extra_info"]["answer"] == r["reward_model"]["ground_truth"] for r in rows),
        }
        quality = Counter()
        for r in rows:
            flags = []
            for parent in ["extra_info", "reward_model"]:
                for k, v in r[parent].items():
                    if v is None:
                        flags.append(f"null:{parent}.{k}")
                    elif isinstance(v, str) and not v.strip():
                        flags.append(f"empty:{parent}.{k}")
            if not r["prompt"]:
                flags.append("empty:prompt")
            if any(not (m["content"] or "").strip() for m in r["prompt"]):
                flags.append("empty:message_content")
            if r["data_source"].endswith("-summarization") and not r["_body"].strip():
                flags.append("empty:article_payload")
            boilerplate = "as a service to our authors and readers , this journal provides supporting information supplied by the authors . such materials are peer reviewed and may be reorganized for online delivery , but are not copyedited or typeset . technical support issues arising from supporting information ( other than missing files ) should be addressed to the authors ."
            if r["data_source"] == "pubmed-summarization" and norm(r["_body"]) == boilerplate:
                flags.append("article_is_publisher_boilerplate")
            roles = [m["role"] for m in r["prompt"]]
            if not roles or roles[-1] != "user":
                flags.append("prompt_not_ending_user")
            if roles != ["user" if i % 2 == 0 else "assistant" for i in range(len(roles))]:
                flags.append("non_alternating_roles")
            if r["prompt"] and r["extra_info"]["question"] != r["prompt"][-1]["content"]:
                flags.append("question_not_last_message")
            for flag in flags:
                quality[flag] += 1
            if flags:
                evidence["quality_flags"].append({**location(r), "flags": flags})
        split_metrics["quality_flags"] = dict(quality)
        split_metrics["prompt_char_thresholds"] = {str(n): sum(r["_prompt_chars"] > n for r in rows) for n in [4000, 8000, 16000, 32000, 64000, 100000]}
        split_metrics["max_prompt_rows"] = [{**location(r), "chars": r["_prompt_chars"]} for r in sorted(rows, key=lambda r: -r["_prompt_chars"])[:10]]
        metrics["by_source"][split] = {}
        for src in sources:
            subset = [r for r in rows if r["data_source"] == src]
            info = metrics["by_source"][split][src] = {
                "n": len(subset), "fraction": len(subset) / len(rows),
                "prompt_chars": summary([r["_prompt_chars"] for r in subset]),
                "answer_chars": summary([r["_answer_chars"] for r in subset]),
                "prompt_char_share": sum(r["_prompt_chars"] for r in subset) / sum(r["_prompt_chars"] for r in rows),
                "message_count": summary([len(r["prompt"]) for r in subset]),
                "style": dict(Counter(r["reward_model"]["style"] for r in subset)),
                "function": dict(Counter(r["extra_info"]["function_call"] for r in subset)),
                "metadata_split": dict(Counter(r["extra_info"]["split"] for r in subset)),
                "row_range": [min(r["_row"] for r in subset), max(r["_row"] for r in subset)],
            }
            if src == "gsm8k":
                info["answers_with_hash_marker"] = sum("####" in r["reward_model"]["ground_truth"] for r in subset)
                info["answers_with_numeric_suffix"] = sum(bool(re.search(r"####\s*[+-]?[\d,]+(?:\.\d+)?\s*$", r["reward_model"]["ground_truth"])) for r in subset)
            if src == "essay_infilling":
                info["blank_marker_counts"] = dict(Counter(r["extra_info"]["question"].count("[fill in the blanks]") - 1 for r in subset))
    print("Profiled fields, roles, and lengths", flush=True)

    keys = {
        "prompt_exact": lambda r: r["prompt"],
        "prompt_normalized": lambda r: [(m["role"], norm(m["content"] or "")) for m in r["prompt"]],
        "question_normalized_nonempty": lambda r: norm(r["extra_info"]["question"]) if r["extra_info"]["question"] and r["extra_info"]["question"].strip() else None,
        "answer_normalized": lambda r: norm(r["reward_model"]["ground_truth"]),
        "sample_without_split_or_function": lambda r: [r["data_source"], r["prompt"], r["reward_model"]],
        "wildchat_first_user_message": lambda r: norm(r["prompt"][0]["content"]) if r["ability"] == "multi-turn" and r["prompt"][0]["content"] else None,
    }
    def translation_pair(r):
        if r["ability"] != "translation":
            return None
        src, tgt = norm(r["_body"]), norm(r["reward_model"]["ground_truth"])
        return [src, tgt] if r["data_source"].endswith("en-fr") else [tgt, src]

    def essay_title(r):
        if r["data_source"] == "essay_abs2text":
            return norm(r["reward_model"]["ground_truth"].splitlines()[0])
        if r["data_source"] == "essay_infilling":
            return norm(r["_body"].splitlines()[0])
        return None

    keys.update(translation_pair_direction_invariant=translation_pair, essay_title_candidate=essay_title)
    metrics["duplicates"] = {}
    for name, key in keys.items():
        metrics["duplicates"][name], evidence[name] = group_audit(all_rows, key)
    prompt_groups = defaultdict(list)
    for r in all_rows:
        prompt_groups[digest(keys["prompt_normalized"](r))].append(r)
    evidence["same_prompt_different_answers"] = [[location(r) for r in g] for g in prompt_groups.values()
                                                if len({norm(r["reward_model"]["ground_truth"]) for r in g}) > 1]
    metrics["same_prompt_different_answer_groups"] = len(evidence["same_prompt_different_answers"])

    # Does a held-out conversation reuse any complete earlier prompt/answer turn?
    turn_index = defaultdict(list)
    for r in all_rows:
        if r["_split"] == "train" and r["ability"] == "multi-turn":
            msgs = r["prompt"] + [{"role": "assistant", "content": r["reward_model"]["ground_truth"]}]
            for i in range(2, len(msgs) + 1, 2):
                turn_index[digest([(m["role"], norm(m["content"] or "")) for m in msgs[:i]])].append(r)
    evidence["wildchat_shared_complete_prefix"] = []
    for r in all_rows:
        if r["_split"] == "valid" and r["ability"] == "multi-turn":
            msgs = r["prompt"] + [{"role": "assistant", "content": r["reward_model"]["ground_truth"]}]
            hits = {}
            for i in range(2, len(msgs) + 1, 2):
                for other in turn_index.get(digest([(m["role"], norm(m["content"] or "")) for m in msgs[:i]]), []):
                    hits[other["_row"]] = {"train": location(other), "valid": location(r), "shared_prefix_messages": i}
            evidence["wildchat_shared_complete_prefix"].extend(hits.values())

    # Keep basic statistics available even if optional plotting/search libraries
    # are still being installed on a fresh machine.
    for name, data in [("metrics.json", metrics), ("evidence.json", evidence)]:
        (args.output_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    from scipy.stats import chi2_contingency, ks_2samp
    counts = np.array([[metrics["by_source"][s][src]["n"] for src in sources] for s in ["train", "valid"]])
    chi, pvalue, dof, _ = chi2_contingency(counts)
    p, q = counts[0] / counts[0].sum(), counts[1] / counts[1].sum()
    mid = (p + q) / 2
    metrics["distribution_comparison"] = {
        "source_total_variation": float(np.abs(p - q).sum() / 2),
        "source_js_divergence_bits": float((np.sum(p * np.log2(p / mid)) + np.sum(q * np.log2(q / mid))) / 2),
        "source_chi_square": {"statistic": float(chi), "pvalue": float(pvalue), "dof": int(dof)},
        "prompt_length_ks_by_source": {},
    }
    for src in sources:
        a = [r["_prompt_chars"] for r in all_rows if r["_split"] == "train" and r["data_source"] == src]
        b = [r["_prompt_chars"] for r in all_rows if r["_split"] == "valid" and r["data_source"] == src]
        result = ks_2samp(a, b)
        metrics["distribution_comparison"]["prompt_length_ks_by_source"][src] = {"statistic": float(result.statistic), "pvalue_unadjusted": float(result.pvalue)}

    # Word unigram/bigram TF-IDF, independently within each source. Templates
    # removed where recognized. This cannot rule out paraphrases/translations.
    from sklearn.feature_extraction.text import TfidfVectorizer
    evidence["nearest_train_prompt_per_valid"] = []
    for src in sources:
        train = [r for r in all_rows if r["_split"] == "train" and r["data_source"] == src]
        valid = [r for r in all_rows if r["_split"] == "valid" and r["data_source"] == src]
        vectorizer = TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, max_features=150000,
                                     strip_accents=None, lowercase=True, dtype=np.float32)
        matrix = vectorizer.fit_transform([r["_body"] for r in train + valid])
        sim = (matrix[len(train):] @ matrix[:len(train)].T).toarray()
        for i, r in enumerate(valid):
            j = int(sim[i].argmax())
            evidence["nearest_train_prompt_per_valid"].append({"train": location(train[j]), "valid": location(r), "cosine": round(float(sim[i, j]), 6)})
    metrics["near_duplicate_screen"] = {str(t): sum(x["cosine"] >= t for x in evidence["nearest_train_prompt_per_valid"]) for t in [.8, .9, .95, .99]}
    # Containment can expose revised/expanded documents even when cosine is
    # moderate. Inspect the strongest retrieved candidates with word 5-grams.
    row_lookup = {(r["_split"], r["_row"]): r for r in all_rows}
    evidence["lexical_overlap_followup"] = []
    def fivegrams(text):
        words = norm(text).split()
        return {tuple(words[i:i + 5]) for i in range(len(words) - 4)}
    for candidate in evidence["nearest_train_prompt_per_valid"]:
        if candidate["cosine"] < .3:
            continue
        a = row_lookup[("train", candidate["train"]["row"])]
        b = row_lookup[("valid", candidate["valid"]["row"])]
        ga, gb = fivegrams(a["_body"]), fivegrams(b["_body"])
        aa, ab = fivegrams(a["reward_model"]["ground_truth"]), fivegrams(b["reward_model"]["ground_truth"])
        evidence["lexical_overlap_followup"].append({**candidate,
            "shared_input_5grams": len(ga & gb), "valid_input_unique_5grams": len(gb),
            "valid_input_5gram_coverage_by_train": len(ga & gb) / len(gb) if gb else None,
            "valid_reference_5gram_coverage_by_train": len(aa & ab) / len(ab) if ab else None})
    print("Checked exact overlaps, conversation/essay groups, and lexical neighbors", flush=True)

    # Full source rows for a small, reproducible manual review, separate from
    # derived quantitative metrics. No automated semantic correctness claim.
    review = []
    for split in ["train", "valid"]:
        subset = [r for r in all_rows if r["_split"] == split and r["data_source"].startswith("tulu")]
        for r in sorted(random.Random(20260923).sample(subset, min(10, len(subset))), key=lambda r: r["_row"]):
            review.append({**location(r), "question": r["extra_info"]["question"], "answer": r["reward_model"]["ground_truth"], "function_call": r["extra_info"]["function_call"]})
    evidence["tulu_manual_review_sample"] = review
    metrics["versions"] = {}
    import importlib.metadata
    for package in ["numpy", "pyarrow", "scipy", "scikit-learn", "matplotlib"]:
        metrics["versions"][package] = importlib.metadata.version(package)
    for name, data in [("metrics.json", metrics), ("evidence.json", evidence)]:
        (args.output_dir / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    make_plot(all_rows, metrics, args.output_dir)
    print(json.dumps({"files": {s: metrics["files"][s]["rows"] for s in ["train", "valid"]},
                      "quality": {s: metrics["by_split"][s]["quality_flags"] for s in ["train", "valid"]},
                      "duplicates": metrics["duplicates"], "near_duplicate_screen": metrics["near_duplicate_screen"]}, ensure_ascii=False, indent=2))


def make_plot(rows, metrics, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    labels = {"gsm8k": "GSM8K", "tulu3-sft-reused-on-policy-8b": "Tulu", "allenai-WildChat-1M-multiturn": "WildChat",
              "essay_abs2text": "Essay writing", "essay_infilling": "Essay infilling", "pubmed-summarization": "PubMed",
              "arxiv-summarization": "arXiv", "govreport-summarization": "GovReport",
              "english-french-translation_en-fr": "EN → FR", "english-french-translation_fr-en": "FR → EN"}
    sources = sorted((s for s in labels if s in metrics["by_source"]["train"]), key=lambda s: -metrics["by_source"]["train"][s]["n"])
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(2, 2, figsize=(14, 10), layout="constrained")
    colors = ["#24738e", "#d77b39"]
    y = np.arange(len(sources))
    ax = axes[0, 0]
    for j, split in enumerate(["train", "valid"]):
        vals = [metrics["by_source"][split][s]["fraction"] * 100 for s in sources]
        ax.barh(y + (j - .5) * .35, vals, height=.34, label=f"{split} (n={metrics['files'][split]['rows']:,})", color=colors[j])
    ax.set(yticks=y, yticklabels=[labels[s] for s in sources], xlabel="Share of rows (%)", title="Source mixture")
    ax.invert_yaxis()
    ax.legend(loc="lower right", fontsize=9)
    ax = axes[0, 1]
    for j, split in enumerate(["train", "valid"]):
        vals = np.sort([r["_prompt_chars"] for r in rows if r["_split"] == split])
        ax.step(vals + 1, np.arange(1, len(vals) + 1) / len(vals), where="post", label=split, color=colors[j], linewidth=2)
    ax.set(xscale="log", xlabel="Prompt characters + 1 (all messages)", ylabel="Cumulative fraction", title="Prompt length distribution — not token counts")
    ax.grid(alpha=.15)
    ax.legend()
    ax = axes[1, 0]
    vals = [metrics["by_source"]["train"][s]["prompt_chars"]["p50"] for s in sources]
    ax.barh(y, vals, color=colors[0])
    ax.set(yticks=y, yticklabels=[labels[s] for s in sources], xscale="log", xlabel="Median prompt characters (log scale)", title="Train input length varies strongly by source")
    ax.invert_yaxis()
    ax = axes[1, 1]
    vals = [metrics["by_source"]["train"][s]["prompt_char_share"] * 100 for s in sources]
    ax.barh(y, vals, color="#567d57")
    ax.set(yticks=y, yticklabels=[labels[s] for s in sources], xlabel="Share of all train prompt characters (%)", title="Text volume differs from sample weighting")
    ax.invert_yaxis()
    fig.suptitle(f"Local RLAR data audit | {metrics['files']['train']['rows']:,} train + {metrics['files']['valid']['rows']:,} valid", fontsize=18, weight="bold")
    fig.savefig(out / "data_overview.png", dpi=160)
    fig.savefig(out / "data_overview.pdf")
    plt.close(fig)


if __name__ == "__main__":
    main()
