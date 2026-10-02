"""Read original sources; answers never enter the canonical policy prompt."""
from __future__ import annotations

from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
import re
import unicodedata


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(obj):
    return hashlib.sha256(dumps(obj).encode()).hexdigest()


def canonical(text):
    return "\n".join(line.rstrip() for line in unicodedata.normalize("NFC", text).replace("\r\n", "\n").splitlines()).strip()


def group_key(messages):
    # Historical alternative assistant responses must not split one task family.
    return digest([{ "role": m["role"], "content": canonical(m["content"]) }
                   for m in messages if m["role"] != "assistant"])


@dataclass
class SourceFile:
    source: str
    split: str
    path: Path
    columns: list[str] | None = None


def source_files(root):
    specs = []
    def add(source, split, pattern, columns=None):
        paths = sorted((root / source).glob(pattern))
        if not paths:
            raise FileNotFoundError(f"Missing {source}/{pattern}")
        specs.extend(SourceFile(source, split, p, columns) for p in paths)
    add("helpsteer2", "train", "train.jsonl.gz")
    add("helpsteer2", "validation", "validation.jsonl.gz")
    add("no_robots", "train", "data/train-*.parquet", ["prompt_id", "messages", "category"])
    add("no_robots", "test", "data/test-*.parquet", ["prompt_id", "messages", "category"])
    add("ultrafeedback", "unsplit", "*.jsonl")
    kc = ["question_id", "question", "subset", "style"]
    add("kodcode", "train", "data/train-*.parquet", kc)
    tc = ["question", "starter_code", "difficulty", "name", "source"]
    add("taco", "train", "ALL/train-*.parquet", tc)
    add("taco", "test", "ALL/test-*.parquet", tc)
    for split in ("train", "dev", "test", "challenge_test"):
        add("mathqa", split, f"data/{split}.json")
    add("bigcodebench_hard", "test", "data/v0.1.4-*.parquet", ["task_id", "instruct_prompt", "complete_prompt"])
    add("classeval", "test", "data/*.parquet", ["task_id", "class_description", "skeleton"])
    add("ds1000", "test", "test.jsonl")
    return specs


def rows(spec, all_columns=False):
    p = spec.path
    if p.suffix == ".parquet":
        import pyarrow.parquet as pq
        i = 0
        for batch in pq.ParquetFile(p).iter_batches(batch_size=128, columns=None if all_columns else spec.columns):
            for row in batch.to_pylist():
                yield i, row
                i += 1
    elif p.suffix == ".json":
        for i, row in enumerate(json.loads(p.read_text())):
            yield i, row
    else:
        opener = gzip.open if p.suffix == ".gz" else open
        with opener(p, "rt", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if line.strip():
                    yield i, json.loads(line)


def normalize(source, row):
    aliases = []
    if source == "helpsteer2":
        text, stratum, orig_id = row["prompt"], "general", None
        messages = [{"role": "user", "content": text}]
    elif source == "no_robots":
        history = row["messages"]
        if not history or history[-1]["role"] != "assistant":
            raise ValueError("No Robots record must end with a demonstration assistant reply")
        messages = [{"role": m["role"], "content": m["content"]} for m in history[:-1]]
        stratum, orig_id = row["category"], row.get("prompt_id")
    elif source == "ultrafeedback":
        messages = [{"role": "user", "content": row["instruction"]}]
        stratum, orig_id = row.get("source", "unknown"), None
    elif source == "kodcode":
        messages = [{"role": "user", "content": row["question"]}]
        stratum, orig_id = row.get("subset", "unknown"), row.get("question_id")
    elif source == "taco":
        text = row["question"]
        if row.get("starter_code"):
            text += "\n\nStarter code:\n" + row["starter_code"]
        messages = [{"role": "user", "content": text}]
        stratum, orig_id = row.get("difficulty", "unknown"), row.get("name")
    elif source == "mathqa":
        messages = [{"role": "user", "content": row["Problem"] + "\n\nOptions:\n" + row["options"]}]
        stratum, orig_id = row.get("category", "unknown"), None
    elif source == "bigcodebench_hard":
        messages = [{"role": "user", "content": row["instruct_prompt"]}]
        aliases = [row["complete_prompt"]]
        stratum, orig_id = "benchmark", row["task_id"]
    elif source == "classeval":
        messages = [{"role": "user", "content": row["class_description"] + "\n" + row["skeleton"]}]
        aliases = [row["skeleton"]]
        stratum, orig_id = "benchmark", row["task_id"]
    elif source == "ds1000":
        messages = [{"role": "user", "content": row["prompt"]}]
        stratum, orig_id = row["metadata"].get("library", "benchmark"), row["metadata"].get("problem_id")
    else:
        raise ValueError(source)
    if not messages or messages[-1]["role"] != "user":
        raise ValueError("Policy prompt must end with a user message")
    for m in messages:
        if m["role"] not in ("system", "user", "assistant") or not isinstance(m["content"], str):
            raise ValueError("Unsupported message structure")
    if not messages[-1]["content"].strip():
        raise ValueError("Empty final user message")
    return {"messages": messages, "group_id": group_key(messages), "stratum": str(stratum or "unknown"),
            "original_id": orig_id, "aliases": aliases}


def family_text(messages):
    return "\n".join(m["content"] for m in messages if m["role"] != "assistant")


def file_sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class NearIndex:
    """Bottom-k token-shingle retrieval plus exact Jaccard confirmation.

    This is a conservative heuristic, not a semantic contamination certificate.
    Full canonical content is retained; only duplicate matching is normalized.
    """
    def __init__(self, threshold=0.9):
        self.threshold, self.items, self.inverted = threshold, {}, {}

    @staticmethod
    def features(text):
        tokens = re.findall(r"\w+|[^\w\s]", text.casefold())
        if len(tokens) < 12:
            return set()
        return {hashlib.blake2b(" ".join(tokens[i:i+3]).encode(), digest_size=8).digest()
                for i in range(len(tokens) - 2)}

    def find(self, features):
        ids = set()
        for key in sorted(features)[:12]:
            ids.update(self.inverted.get(key, ()))
        for ident in sorted(ids):
            other = self.items[ident]
            if min(len(features), len(other)) < self.threshold * max(len(features), len(other)):
                continue
            score = len(features & other) / len(features | other)
            if score >= self.threshold:
                return ident, score
        return None

    def add(self, ident, features):
        if not features:
            return
        self.items[ident] = features
        for key in sorted(features)[:12]:
            self.inverted.setdefault(key, set()).add(ident)
