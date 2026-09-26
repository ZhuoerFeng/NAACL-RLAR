#!/usr/bin/env python3
"""Standalone diagnostic figure; task and token estimates are clearly labeled."""
import json
from pathlib import Path
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pyarrow.parquet as pq

OUT = Path(__file__).resolve().parent / "results/nemotron_sampling_review"
LABELS = {"translation":"Translation", "summarization":"Summarization", "rewrite_edit":"Rewrite / edit", "code_technical":"Code / technical", "math_quantitative":"Math / quantitative", "extraction_format":"Extraction / format", "creative_generation":"Creative writing", "planning_advice":"Planning / advice", "knowledge_explanation":"Knowledge / explanation", "other_uncertain":"Unclassified / uncertain"}

def main():
    while not (OUT / "diagnostic_metrics.json").exists():
        time.sleep(5)
    metrics = json.loads((OUT / "full_audit_metrics.json").read_text())
    rows = pq.read_table(OUT / "diagnostic_token_lengths.parquet").to_pylist()
    plt.rcParams.update({"font.family":"DejaVu Sans", "font.size":11, "axes.spines.top":False, "axes.spines.right":False})
    fig, axes = plt.subplots(1,2,figsize=(14,6.5),layout="constrained")
    sections = ["reasoning_off", "reasoning_on"]
    colors = ["#28758a", "#c5783f"]
    keys = sorted(LABELS,key=lambda k: -metrics["sections"]["reasoning_off"]["task_hints"].get(k,0))
    for i, (section, color) in enumerate(zip(sections, colors)):
        s = metrics["sections"][section]
        values = [100*s["task_hints"].get(k,0)/s["rows"] for k in keys]
        axes[0].barh(np.arange(len(keys)) + (i-.5)*.36, values, height=.34, color=color, label=f"{section} (n={s['rows']:,})")
        tokens = np.sort([r["prompt_tokens_thinking"] for r in rows if r["section"]==section])
        axes[1].step(tokens,100*np.arange(1,len(tokens)+1)/len(tokens),where="post",color=color,label=f"{section} (diagnostic n={len(tokens):,})")
    axes[0].set(yticks=np.arange(len(keys)),yticklabels=[LABELS[k] for k in keys],xlabel="Share of records (%)",title="Full scan: keyword task hints\nHeuristic labels, not semantic ground truth")
    axes[0].invert_yaxis()
    axes[0].legend(loc="lower right",fontsize=9)
    axes[1].set(xscale="log",xlabel="Full prompt tokens · Qwen chat template",ylabel="Cumulative share (%)",title="Random diagnostic sample: prompt length\nNo truncation or length filtering")
    axes[1].axvline(10000,color="#666666",linestyle="--",linewidth=1,label="10,000 tokens")
    axes[1].legend(loc="lower right",fontsize=9)
    axes[1].grid(alpha=.15)
    fig.suptitle("Nemotron Instruction-Following Chat v2 · pre-sampling review",fontsize=15)
    fig.savefig(OUT / "dataset_overview.png",dpi=180)
    fig.savefig(OUT / "dataset_overview.pdf")
    plt.close(fig)
    print("Saved dataset_overview.png and .pdf")

if __name__ == "__main__":
    main()
