#!/usr/bin/env python3
"""Measure full Qwen chat-template token lengths without truncating/filtering."""
import argparse
from collections import Counter
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path

os.environ.setdefault("RAYON_NUM_THREADS", "4")
import numpy as np
import pyarrow.parquet as pq
from transformers import AutoTokenizer

from analyze_train_valid import summary

THRESHOLDS = [4096, 8192, 10000, 16384, 32768, 40960, 65536, 131072]


def sha256(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def describe(rows):
    return {"n": len(rows),
            "prompt_thinking": summary([r["prompt_tokens_thinking"] for r in rows]),
            "prompt_nonthinking": summary([r["prompt_tokens_nonthinking"] for r in rows]),
            "reference_text": summary([r["reference_tokens"] for r in rows]),
            "prompt_over_threshold_thinking": {str(t): sum(r["prompt_tokens_thinking"] > t for r in rows) for t in THRESHOLDS},
            "prompt_over_threshold_nonthinking": {str(t): sum(r["prompt_tokens_nonthinking"] > t for r in rows) for t in THRESHOLDS},
            "reference_over_5000": sum(r["reference_tokens"] > 5000 for r in rows)}


def main():
    base = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=base / "data")
    parser.add_argument("--tokenizer-dir", type=Path, default=base / "model_tokeniser")
    parser.add_argument("--output-dir", type=Path, default=base / "analysis/results/cleanup_20260923")
    args = parser.parse_args()
    out = args.output_dir
    out.mkdir(parents=True, exist_ok=True)
    tok = AutoTokenizer.from_pretrained(str(args.tokenizer_dir), local_files_only=True)
    config = json.loads((args.tokenizer_dir / "config.json").read_text())
    mapping_path = out / "retained_row_map.json"
    old_indices = json.loads(mapping_path.read_text()) if mapping_path.exists() else {}
    result = {"tokenizer_path": str(args.tokenizer_dir.resolve()), "tokenizer_class": type(tok).__name__,
              "tokenizer_hashes": {p.name: sha256(p) for p in sorted(args.tokenizer_dir.glob("*.json"))},
              "versions": {p: importlib.metadata.version(p) for p in ["transformers", "tokenizers", "jinja2"]},
              "tokenizer_model_max_length": tok.model_max_length,
              "model_config_max_position_embeddings": config.get("max_position_embeddings"),
              "prompt_method": "apply_chat_template(full prompt, tokenize=True, add_generation_prompt=True, enable_thinking=True/False, truncation=False, padding=False); no additional system prompt or tools",
              "reference_method": "tokenizer(ground_truth, add_special_tokens=False, truncation=False, padding=False); no generated reasoning assumed",
              "action": "Statistics only; no token-based filtering or truncation", "splits": {}}
    all_lengths = []
    for split in ["train", "valid"]:
        path = args.data_dir / f"{split}.parquet"
        rows = pq.read_table(path).to_pylist()
        if split in old_indices:
            assert len(old_indices[split]) == len(rows), "Row mapping does not match input"
        lengths = []
        for start in range(0, len(rows), 64):
            chunk = rows[start:start + 64]
            prompts = [r["prompt"] for r in chunk]
            tokens_on = tok.apply_chat_template(prompts, tokenize=True, add_generation_prompt=True,
                                               enable_thinking=True, truncation=False, padding=False)
            tokens_off = tok.apply_chat_template(prompts, tokenize=True, add_generation_prompt=True,
                                                enable_thinking=False, truncation=False, padding=False)
            ref = tok([r["reward_model"]["ground_truth"] for r in chunk],
                      add_special_tokens=False, truncation=False, padding=False)["input_ids"]
            for j, row in enumerate(chunk):
                idx = start + j
                lengths.append({"split": split, "row": idx, "original_row": old_indices.get(split, list(range(len(rows))))[idx],
                                "source": row["data_source"], "prompt_tokens_thinking": len(tokens_on[j]),
                                "prompt_tokens_nonthinking": len(tokens_off[j]), "reference_tokens": len(ref[j])})
            if start % 1024 == 0:
                print(f"{split}: {min(start + 64, len(rows))}/{len(rows)}", flush=True)
        result["splits"][split] = {"input_sha256": sha256(path), **describe(lengths),
                                   "nonthinking_minus_thinking": dict(Counter(r["prompt_tokens_nonthinking"] - r["prompt_tokens_thinking"] for r in lengths)),
                                   "by_source": {src: describe([r for r in lengths if r["source"] == src]) for src in sorted({r["source"] for r in lengths})},
                                   "longest_prompts": sorted(lengths, key=lambda r: -r["prompt_tokens_thinking"])[:10]}
        all_lengths.extend(lengths)
    (out / "qwen_token_metrics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    (out / "qwen_token_lengths.json").write_text(json.dumps(all_lengths, ensure_ascii=False, indent=2) + "\n")
    plot(all_lengths, result, out)
    print(json.dumps({s: {k: v for k, v in r.items() if k not in ["by_source", "longest_prompts"]} for s, r in result["splits"].items()}, indent=2))


def plot(lengths, metrics, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    labels = {"gsm8k": "GSM8K", "allenai-WildChat-1M-multiturn": "WildChat",
              "essay_abs2text": "Essay writing", "essay_infilling": "Essay infilling", "pubmed-summarization": "PubMed",
              "arxiv-summarization": "arXiv", "govreport-summarization": "GovReport",
              "english-french-translation_en-fr": "EN → FR", "english-french-translation_fr-en": "FR → EN"}
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11,
                         "axes.spines.top": False, "axes.spines.right": False})
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.4), layout="constrained")
    colors = ["#24738e", "#d77b39"]
    ax = axes[0]
    for i, split in enumerate(["train", "valid"]):
        values = np.sort([r["prompt_tokens_thinking"] for r in lengths if r["split"] == split])
        ax.step(values, np.arange(1, len(values) + 1) / len(values), where="post", label=f"{split} (n={len(values):,})", color=colors[i])
    ax.axvline(10000, color="#333333", linestyle="--", alpha=.7, label="10,000 tokens")
    ax.set(xscale="log", xlabel="Prompt tokens including chat template", ylabel="Cumulative fraction", title="Qwen tokenizer · thinking enabled")
    ax.legend(loc="lower right")
    ax.grid(alpha=.15)
    ax = axes[1]
    sources = sorted(metrics["splits"]["train"]["by_source"], key=lambda s: -metrics["splits"]["train"]["by_source"][s]["prompt_over_threshold_thinking"]["10000"])
    for i, split in enumerate(["train", "valid"]):
        vals = [100 * metrics["splits"][split]["by_source"][s]["prompt_over_threshold_thinking"]["10000"] / metrics["splits"][split]["by_source"][s]["n"] for s in sources]
        ax.barh(np.arange(len(sources)) + (i - .5) * .35, vals, height=.34, color=colors[i], label=split)
    ax.set(yticks=np.arange(len(sources)), yticklabels=[labels[s] for s in sources], xlabel="Rows over 10,000 prompt tokens (%)", title="Length screening only · rows retained")
    ax.invert_yaxis()
    ax.legend()
    fig.savefig(out / "qwen_token_overview.png", dpi=160)
    fig.savefig(out / "qwen_token_overview.pdf")
    plt.close(fig)


if __name__ == "__main__":
    main()
