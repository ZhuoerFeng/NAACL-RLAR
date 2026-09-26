#!/usr/bin/env python3
"""Apply the four user-approved row filters, preserving all retained values.

Default is a dry run; --apply makes verified backups and replaces train/valid.
No filtering by length, function_call, translation direction, or similarity.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil

import pyarrow as pa
import pyarrow.parquet as pq

from analyze_train_valid import body, norm

BOILERPLATE = (
    "as a service to our authors and readers , this journal provides supporting information supplied by the authors . "
    "such materials are peer reviewed and may be reorganized for online delivery , but are not copyedited or typeset . "
    "technical support issues arising from supporting information ( other than missing files ) should be addressed to the authors ."
)


def sha256(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def removal_reasons(row):
    reasons = []
    source = row["data_source"]
    if source == "tulu3-sft-reused-on-policy-8b":
        reasons.append("remove_tulu_subset")
    if source == "pubmed-summarization":
        article = norm(body(row))
        if not article:
            reasons.append("pubmed_empty_article")
        elif article == BOILERPLATE:
            reasons.append("pubmed_publisher_boilerplate_only")
    if source == "allenai-WildChat-1M-multiturn":
        users = [m for m in row["prompt"] if m["role"] == "user"]
        if users and not (users[-1]["content"] or "").strip():
            reasons.append("wildchat_empty_last_user_message")
    return reasons


def main():
    base = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=base / "data")
    parser.add_argument("--output-dir", type=Path, default=base / "analysis/results/cleanup_20260923")
    parser.add_argument("--backup-dir", type=Path, default=base / "data/backups/pre_cleanup_20260923")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    data_dir, out, backup = (p.resolve() for p in [args.data_dir, args.output_dir, args.backup_dir])
    manifest = {"created_utc": datetime.now(timezone.utc).isoformat(), "status": "dry_run",
                "data_dir": str(data_dir), "backup_dir": str(backup),
                "filters": ["remove_tulu_subset", "pubmed_empty_article", "pubmed_publisher_boilerplate_only", "wildchat_empty_last_user_message"],
                "unchanged_policy": "Retain empty function_call, translation pairs in both directions, GovReport overlap, long prompts, and historical empty messages when the last user message is nonempty.",
                "splits": {}}
    prepared, removals, row_map = {}, [], {}
    for split in ["train", "valid"]:
        path = data_dir / f"{split}.parquet"
        checksum = sha256(path)
        original = pq.read_table(path)
        rows = original.to_pylist()
        keep, reason_counts = [], Counter()
        for i, row in enumerate(rows):
            reasons = removal_reasons(row)
            if reasons:
                removals.append({"split": split, "original_row": i, "source": row["data_source"], "reasons": reasons})
                reason_counts.update(reasons)
            else:
                keep.append(i)
        filtered = original.take(pa.array(keep, type=pa.int64()))
        assert filtered.schema.equals(original.schema, check_metadata=True)
        assert not any(removal_reasons(row) for row in filtered.to_pylist())
        prepared[split] = (original, filtered)
        row_map[split] = keep
        manifest["splits"][split] = {"before": len(rows), "removed": len(rows) - len(keep), "after": len(keep),
                                     "reason_counts": dict(reason_counts), "original_sha256": checksum,
                                     "source_counts_before": dict(Counter(r["data_source"] for r in rows)),
                                     "source_counts_after": dict(Counter(rows[i]["data_source"] for i in keep))}
    if not args.apply:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return
    if not removals:
        print("Already clean under the approved rules; no files changed.")
        return
    if backup.exists() or (out / "cleanup_manifest.json").exists():
        raise FileExistsError("Existing backup/manifest: choose new paths instead of overwriting audit history.")
    backup.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)
    staged = {}
    for split, (original, filtered) in prepared.items():
        target = data_dir / f"{split}.parquet"
        assert sha256(target) == manifest["splits"][split]["original_sha256"], "Input changed during preparation"
        shutil.copy2(target, backup / target.name)
        assert sha256(backup / target.name) == manifest["splits"][split]["original_sha256"]
        temp = data_dir / f".{split}.cleanup-staged.parquet"
        if temp.exists():
            raise FileExistsError(temp)
        pq.write_table(filtered, temp, compression="snappy")
        reread = pq.read_table(temp)
        assert reread.equals(filtered, check_metadata=True), "Round-trip changed retained content or schema"
        manifest["splits"][split]["cleaned_sha256"] = sha256(temp)
        staged[split] = temp
    (out / "removed_rows.json").write_text(json.dumps(removals, ensure_ascii=False, indent=2) + "\n")
    (out / "retained_row_map.json").write_text(json.dumps(row_map, indent=2) + "\n")
    manifest["status"] = "prepared"
    manifest_path = out / "cleanup_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    committed = []
    try:
        for split in ["train", "valid"]:
            target = data_dir / f"{split}.parquet"
            assert sha256(target) == manifest["splits"][split]["original_sha256"], "Input changed before commit"
            os.replace(staged[split], target)
            committed.append(split)
            assert sha256(target) == manifest["splits"][split]["cleaned_sha256"]
        manifest["status"] = "applied_and_verified"
    except Exception:
        for split in committed:
            shutil.copy2(backup / f"{split}.parquet", data_dir / f"{split}.parquet")
        manifest["status"] = "rolled_back"
        raise
    finally:
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
