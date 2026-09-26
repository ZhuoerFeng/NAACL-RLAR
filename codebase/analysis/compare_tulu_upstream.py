#!/usr/bin/env python3
"""Compare local Tulu examples with the pinned, downloaded upstream parquet."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path

import pyarrow.parquet as pq

from analyze_train_valid import norm


def main():
    root = Path(__file__).resolve().parents[1]
    raw = root / "data/raw/tulu3-sft-reused-on-policy-8b"
    manifest = json.loads((raw / "download_manifest.json").read_text())
    upstream = pq.read_table(raw / "train-00000-of-00001.parquet").to_pylist()
    prompts, answers = defaultdict(list), defaultdict(list)
    structure = Counter()
    for i, row in enumerate(upstream):
        prompts[norm(row["prompt"])].append(i)
        for choice in ["chosen", "rejected"]:
            messages = row[choice]
            structure[f"{choice}_role_pattern:" + ",".join(m["role"] for m in messages)] += 1
            if messages[0]["content"] == row["prompt"]:
                structure[f"{choice}_first_message_equals_prompt"] += 1
            if messages[-1]["role"] == "assistant":
                answers[norm(messages[-1]["content"])].append((i, choice))
    summary, mappings = {}, []
    for split in ["train", "valid"]:
        original = root / "data" / f"{split}.parquet"
        counts, offsets = Counter(), Counter()
        for row_idx, row in enumerate(pq.read_table(original).to_pylist()):
            if row["data_source"] != "tulu3-sft-reused-on-policy-8b":
                continue
            counts["local_rows"] += 1
            question = norm(row["extra_info"]["question"])
            answer = norm(row["reward_model"]["ground_truth"])
            qids, aids = prompts.get(question, []), answers.get(answer, [])
            same = [(i, side) for i, side in aids if i in qids]
            if qids:
                counts["question_found_upstream"] += 1
            if len(qids) == 1:
                counts["question_unique_upstream"] += 1
            if aids:
                counts["answer_found_anywhere_upstream"] += 1
            if same:
                counts["answer_matches_same_upstream_question"] += 1
                for side in set(side for _, side in same):
                    counts[f"answer_matches_same_question_{side}"] += 1
            elif qids and aids:
                counts["answer_belongs_to_different_upstream_question"] += 1
            elif qids:
                counts["question_found_but_answer_not_upstream"] += 1
            if len(qids) == 1 and len({i for i, _ in aids}) == 1:
                offsets[aids[0][0] - qids[0]] += 1
            mappings.append({"split": split, "local_row": row_idx,
                             "question_upstream_rows": qids,
                             "question_upstream_ids": [upstream[i]["id"] for i in qids],
                             "answer_upstream_matches": [{"row": i, "id": upstream[i]["id"], "side": side} for i, side in aids],
                             "answer_matches_same_question": bool(same)})
        with original.open("rb") as f:
            checksum = hashlib.file_digest(f, "sha256").hexdigest()
        summary[split] = {"counts": dict(counts), "original_sha256": checksum, "most_common_answer_minus_question_offsets": offsets.most_common(10)}
    result = {"upstream": manifest, "upstream_structural_checks": dict(structure),
              "upstream_unique_normalized_prompts": len(prompts), "comparison": summary,
              "method": "NFC normalization plus whitespace collapse; compare full local reference to final upstream chosen/rejected assistant messages. Structural alignment is not a guarantee of answer correctness.",
              "row_mappings": mappings}
    out = root / "analysis/results/tulu_upstream_comparison.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "row_mappings"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
