from __future__ import annotations

from collections import Counter, defaultdict, deque
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
from pathlib import Path
import shutil
import sqlite3
from zoneinfo import ZoneInfo

from .models import Classification, SelectionConfig, SOURCES
from .sources import NearIndex, digest, dumps, family_text, file_sha256, group_key, normalize, rows, source_files


def write_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def write_jsonl(path, rows_):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        for row in rows_:
            f.write(dumps(row) + "\n")
    tmp.replace(path)


def connect(run):
    db = sqlite3.connect(run / "selection.sqlite")
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    return db


@contextmanager
def run_lock(run):
    with (run / ".selection.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another command is using this selection run") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def new_run(project):
    parent = project / "codebase/reward_harness/runs"
    parent.mkdir(parents=True, exist_ok=True)
    prefix = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y_%m_%d_%H_%M_DatasetSelection")
    run = parent / prefix
    if run.exists():
        import secrets
        run = parent / (prefix + secrets.token_hex(3).capitalize())
    run.mkdir()
    return run


def schema(db):
    db.executescript("""
    CREATE TABLE records(gid TEXT PRIMARY KEY, source TEXT, stratum TEXT, draw TEXT,
                         messages TEXT, family TEXT, pool_position INTEGER,
                         disposition TEXT NOT NULL DEFAULT 'available', detail TEXT);
    CREATE INDEX source_draw ON records(source, stratum, draw);
    CREATE INDEX disposition ON records(disposition,source);
    CREATE TABLE members(gid TEXT, source TEXT, split TEXT, path TEXT, row_index INTEGER, original_id TEXT);
    CREATE INDEX member_gid ON members(gid);
    CREATE TABLE reserved(gid TEXT PRIMARY KEY, source TEXT, split TEXT, family TEXT);
    CREATE TABLE calls(cache_key TEXT PRIMARY KEY, gid TEXT, status TEXT, request_path TEXT,
                       response_path TEXT, label TEXT, error TEXT);
    CREATE INDEX call_gid ON calls(gid);
    """)


def run_config(run):
    state = json.loads((run / "manifest.json").read_text())
    cfg = SelectionConfig.model_validate(json.loads((run / "config.json").read_text()))
    if digest(cfg.model_dump(mode="json")) != state["config_digest"]:
        raise ValueError("Frozen configuration changed; create a new run")
    if file_sha256(run / "classifier_prompt.txt") != state["prompt_sha256"]:
        raise ValueError("Frozen classifier prompt changed; create a new run")
    if digest(Classification.model_json_schema()) != state["schema_digest"]:
        raise ValueError("Classification schema changed; create a new run")
    if state["phase"] == "indexing":
        raise ValueError("Indexing was interrupted; create a new run rather than using a partial index")
    return cfg, state


def check_inputs(run):
    for spec in json.loads((run / "source_fingerprints.json").read_text()):
        p = Path(spec["path"])
        if p.stat().st_size != spec["bytes"] or file_sha256(p) != spec["sha256"]:
            raise ValueError(f"Source file changed: {p}")


def fork_classifier(parent, cfg):
    """Reuse a frozen candidate pool with a new classifier configuration.

    Parent evidence remains untouched. No prior labels or calls transfer to the new run.
    """
    old, state = run_config(parent)
    before, after = old.model_dump(mode="json"), cfg.model_dump(mode="json")
    before.pop("classifier")
    after.pop("classifier")
    if before != after:
        raise ValueError("fork-classifier only permits classifier changes; sampling changes need prepare")
    check_inputs(parent)
    run = new_run(Path(state["project_root"]))
    for name in ("source_fingerprints.json", "source_inventory.json", "structural_exclusions.jsonl",
                 "classifier_prompt.txt", "classification_schema.json", "candidates.jsonl", "pool_report.json"):
        shutil.copyfile(parent / name, run / name)
    write_json(run / "config.json", cfg.model_dump(mode="json"))
    source = sqlite3.connect(f"file:{parent / 'selection.sqlite'}?mode=ro", uri=True)
    target = connect(run)
    try:
        source.backup(target)
        target.execute("DELETE FROM calls")
        target.execute("UPDATE records SET disposition='pending',detail=NULL WHERE pool_position IS NOT NULL")
        target.commit()
    finally:
        source.close()
        target.close()
    state.update(phase="prepared", parent_run=str(parent),
                 config_digest=digest(cfg.model_dump(mode="json")),
                 created_local=datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
                 code_fingerprints={p.name: file_sha256(p) for p in Path(__file__).parent.glob("*.py")})
    write_json(run / "manifest.json", state)
    return run


def prepare(project, cfg):
    root = project / "codebase/data/datasets"
    specs = source_files(root)
    run = new_run(project)
    write_json(run / "config.json", cfg.model_dump(mode="json"))
    prompt = Path(__file__).with_name("classifier_prompt.txt").read_text()
    (run / "classifier_prompt.txt").write_text(prompt)
    write_json(run / "classification_schema.json", Classification.model_json_schema())
    state = {"phase": "indexing", "config_digest": digest(cfg.model_dump(mode="json")),
             "prompt_sha256": file_sha256(run / "classifier_prompt.txt"),
             "schema_digest": digest(Classification.model_json_schema()),
             "project_root": str(project), "dataset_root": str(root),
             "created_local": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
             "normalizer_version": 1,
             "code_fingerprints": {p.name: file_sha256(p) for p in Path(__file__).parent.glob("*.py")},
             "notes": ["No filtering on reference quality or policy ability; no manual review stage.",
                       "Official heldout splits and three benchmark datasets excluded from training.",
                       "UltraFeedback has a deterministic 20% group holdout by default; configurable before run creation.",
                       "KodCode use_with_caution is not a training source."]}
    write_json(run / "manifest.json", state)
    fingerprints = []
    for spec in specs:
        fingerprints.append({"source": spec.source, "split": spec.split, "path": str(spec.path),
                             "bytes": spec.path.stat().st_size, "sha256": file_sha256(spec.path)})
    write_json(run / "source_fingerprints.json", fingerprints)
    print(dumps({"run": str(run), "stage": "indexing", "files": len(specs)}), flush=True)
    db = connect(run)
    schema(db)
    counts = Counter()
    with (run / "structural_exclusions.jsonl").open("w") as bad:
        for spec in specs:
            for idx, raw in rows(spec):
                counts[f"{spec.source}/{spec.split}/rows"] += 1
                try:
                    item = normalize(spec.source, raw)
                except (ValueError, KeyError, TypeError) as exc:
                    bad.write(dumps({"source": spec.source, "split": spec.split, "path": str(spec.path),
                                     "row_index_0based": idx, "reason": type(exc).__name__, "detail": str(exc)}) + "\n")
                    counts[f"{spec.source}/invalid"] += 1
                    continue
                gid = item["group_id"]
                split = spec.split
                if split == "unsplit":
                    u = int(digest([cfg.seed, "holdout", gid])[:16], 16) / 2**64
                    split = "holdout" if u < cfg.ultrafeedback_holdout_fraction else "train"
                family = family_text(item["messages"])
                if split != "train":
                    db.execute("INSERT OR IGNORE INTO reserved VALUES(?,?,?,?)", (gid, spec.source, split, family))
                    for alias in item["aliases"]:
                        agid = group_key([{"role": "user", "content": alias}])
                        db.execute("INSERT OR IGNORE INTO reserved VALUES(?,?,?,?)", (agid, spec.source, split, alias))
                    continue
                db.execute("INSERT INTO members VALUES(?,?,?,?,?,?)", (gid, spec.source, split, str(spec.path), idx,
                           None if item["original_id"] is None else str(item["original_id"])))
                db.execute("INSERT OR IGNORE INTO records(gid,source,stratum,draw,messages,family) VALUES(?,?,?,?,?,?)",
                           (gid, spec.source, item["stratum"], digest([cfg.seed, "draw", gid]), dumps(item["messages"]), family))
                if idx % 5000 == 0:
                    db.commit()
            db.commit()
            print(dumps({"indexed": str(spec.path.relative_to(root)), "rows": counts[f"{spec.source}/{spec.split}/rows"]}), flush=True)
    db.execute("UPDATE records SET disposition='reserved_exact',detail='Exact task family occurs in a heldout split' WHERE gid IN (SELECT gid FROM reserved)")
    db.commit()
    state["phase"] = "prepared"
    write_json(run / "manifest.json", state)
    write_json(run / "source_inventory.json", dict(counts))
    extend_pool(run, db, cfg)
    db.close()
    return run


def build_near(db, threshold):
    index = NearIndex(threshold)
    for r in db.execute("SELECT gid,family FROM reserved ORDER BY gid"):
        index.add("heldout:" + r["gid"], index.features(r["family"]))
    for r in db.execute("SELECT gid,family FROM records WHERE pool_position IS NOT NULL ORDER BY pool_position"):
        index.add("candidate:" + r["gid"], index.features(r["family"]))
    return index


def extend_pool(run, db, cfg, sources=None):
    index = build_near(db, cfg.near_duplicate_threshold)
    added = Counter()
    pos = db.execute("SELECT COALESCE(MAX(pool_position),-1)+1 FROM records").fetchone()[0]
    for source in (sources or SOURCES):
        target = cfg.targets.get(source, 0) * cfg.candidate_multiplier
        if not target:
            continue
        strata = [r[0] for r in db.execute("SELECT DISTINCT stratum FROM records WHERE source=? AND disposition='available' ORDER BY stratum", (source,))]
        cursors = deque(db.execute("SELECT gid,messages,family FROM records WHERE source=? AND stratum=? AND disposition='available' ORDER BY draw",
                                   (source, s)) for s in strata)
        while cursors and added[source] < target:
            cursor = cursors.popleft()
            r = cursor.fetchone()
            if r is None:
                continue
            cursors.append(cursor)
            if len(r["messages"]) > cfg.classifier_max_input_chars:
                db.execute("UPDATE records SET disposition='deferred_input_limit',detail=? WHERE gid=?",
                           ("Classifier input limit; no truncation and not a task-quality exclusion", r["gid"]))
                continue
            features = index.features(r["family"])
            match = index.find(features)
            if match:
                db.execute("UPDATE records SET disposition='near_duplicate',detail=? WHERE gid=?", (dumps({"matched": match[0], "jaccard": match[1]}), r["gid"]))
                continue
            db.execute("UPDATE records SET disposition='pending',pool_position=? WHERE gid=?", (pos, r["gid"]))
            index.add("candidate:" + r["gid"], features)
            added[source] += 1
            pos += 1
        db.commit()
    write_jsonl(run / "candidates.jsonl", (dict(r) | {"messages": json.loads(r["messages"])} for r in db.execute(
        "SELECT gid,source,stratum,messages,pool_position FROM records WHERE pool_position IS NOT NULL ORDER BY pool_position")))
    write_json(run / "pool_report.json", {
        "added": dict(added), "dispositions": [dict(r) for r in db.execute("SELECT source,disposition,COUNT(*) AS count FROM records GROUP BY source,disposition")],
        "heldout_families": db.execute("SELECT COUNT(*) FROM reserved").fetchone()[0],
        "dedup_limit": "Canonical exact families over full sources; token-shingle near-duplicate heuristic over heldouts and candidate pools, not semantic proof."})
    return sum(added.values())


def accepted_counts(db):
    return dict(db.execute("SELECT source,COUNT(*) FROM records WHERE disposition='include' GROUP BY source").fetchall())


def status(run):
    """Read progress without taking the writer lock or mutating an active run."""
    cfg, state = run_config(run)
    db = sqlite3.connect(f"file:{run / 'selection.sqlite'}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        accepted = accepted_counts(db)
        return {"run": str(run), "model": cfg.classifier.model, "target_total": sum(cfg.targets.values()),
                "accepted_total": sum(accepted.values()), "accepted_by_source": accepted,
                "targets": cfg.targets, "calls_by_status": dict(db.execute("SELECT status,COUNT(*) FROM calls GROUP BY status")),
                "unresolved_service_errors": db.execute("SELECT COUNT(*) FROM records WHERE disposition='service_error'").fetchone()[0],
                "last_export_phase": state["phase"]}
    finally:
        db.close()


def next_batch(db, cfg, size):
    accepted = accepted_counts(db)
    picked = []
    # Round-robin across sources, then native strata, to avoid a source-ordered probe.
    queues = {}
    for source in SOURCES:
        need = cfg.targets.get(source, 0) - accepted.get(source, 0)
        if need > 0:
            queues[source] = deque(db.execute("SELECT * FROM records WHERE source=? AND disposition='pending' ORDER BY pool_position LIMIT ?",
                                             (source, min(need, size))).fetchall())
    while len(picked) < size and any(queues.values()):
        for source in SOURCES:
            if queues.get(source) and len(picked) < size:
                picked.append(dict(queues[source].popleft()))
    return picked


def apply_label(db, row, label, cfg):
    disposition = label.decision
    detail = None
    if disposition == "include" and cfg.general_sources_only and row["source"] in ("helpsteer2", "no_robots", "ultrafeedback"):
        if label.task_type in ("coding", "math_problem"):
            disposition, detail = "outside_general_scope", "Substantive task routed outside the general-task training source quota"
    db.execute("UPDATE records SET disposition=?,detail=? WHERE gid=?", (disposition, detail, row["gid"]))


def export(run):
    cfg, state = run_config(run)
    db = connect(run)
    selected = []
    for source in SOURCES:
        selected.extend(dict(r) for r in db.execute(
            "SELECT * FROM records WHERE source=? AND disposition='include' ORDER BY pool_position LIMIT ?", (source, cfg.targets.get(source, 0))))
    # Stable global shuffle; no answer/reference fields in policy input.
    selected.sort(key=lambda r: digest([cfg.seed, "export", r["gid"]]))
    out = run / "exports"
    out.mkdir(exist_ok=True)
    prompts = [{"sample_id": r["gid"], "data_source": r["source"], "prompt": json.loads(r["messages"])} for r in selected]
    write_jsonl(out / "train_prompts.jsonl", prompts)
    import pyarrow as pa
    import pyarrow.parquet as pq
    if prompts:
        table = pa.Table.from_pylist(prompts)
    else:
        table = pa.table({"sample_id": pa.array([], type=pa.string()), "data_source": pa.array([], type=pa.string()),
                          "prompt": pa.array([], type=pa.list_(pa.struct([("role", pa.string()), ("content", pa.string())])))})
    tmp = out / "train_prompts.parquet.tmp"
    pq.write_table(table, tmp)
    tmp.replace(out / "train_prompts.parquet")
    write_jsonl(out / "evaluation_assets.jsonl", (
        {"sample_id": r["gid"], "source_records": [dict(m) for m in db.execute("SELECT source,split,path,row_index AS row_index_0based,original_id FROM members WHERE gid=? ORDER BY source,path,row_index", (r["gid"],))],
         "selected_prompt_sha256": digest(json.loads(r["messages"])), "source_records_scope": "task_family",
         "reference_policy": "Read original rows using source_fingerprints.json. Re-normalize each row and only apply reference/labels when digest(normalized_messages) matches selected_prompt_sha256; alternative historical assistant contexts are provenance only. Never pass reference records to the classifier or policy."}
        for r in selected))
    write_jsonl(out / "classification_labels.jsonl", (
        {"sample_id": r["gid"], "classification": json.loads(db.execute("SELECT label FROM calls WHERE gid=? AND status='complete'", (r["gid"],)).fetchone()[0])}
        for r in selected))
    write_jsonl(out / "uncertain.jsonl", (dict(r) | {"messages": json.loads(r["messages"])} for r in db.execute(
        "SELECT gid,source,messages FROM records WHERE disposition='uncertain' ORDER BY pool_position")))
    write_json(out / "rollout_contract.json", {"thinking": False, "max_new_tokens": 8000,
               "generation_performed": False, "truncation_policy": "Record finish_reason=length; do not assume truncated outputs are complete answers.",
               "classifier_dimensions_are_not_reward_rubrics": True})
    counts = Counter(r["source"] for r in selected)
    gaps = {s: n - counts[s] for s, n in cfg.targets.items() if n > counts[s]}
    checks = {"unique_sample_ids": len({r["gid"] for r in selected}) == len(selected),
              "no_exact_heldout_overlap": all(not db.execute("SELECT 1 FROM reserved WHERE gid=?", (r["gid"],)).fetchone() for r in selected),
              "only_official_train_or_assigned_ultrafeedback_train": all(
                  m["split"] == "train" and m["source"] in SOURCES and "use_with_caution" not in m["path"]
                  for r in selected for m in db.execute("SELECT source,split,path FROM members WHERE gid=?", (r["gid"],))),
              "no_final_reference_in_policy_export": all(set(p) == {"sample_id", "data_source", "prompt"} and p["prompt"][-1]["role"] == "user" for p in prompts)}
    report = {"status": "complete" if not gaps else "incomplete", "target_total": sum(cfg.targets.values()),
              "selected_total": len(selected), "selected_by_source": dict(counts), "shortfalls": gaps,
              "selected_by_task_type": {}, "checks": checks,
              "classifier_model": cfg.classifier.model,
              "calls_by_status": dict(db.execute("SELECT status,COUNT(*) FROM calls GROUP BY status").fetchall()),
              "dispositions": [dict(r) for r in db.execute("SELECT source,disposition,COUNT(*) AS count FROM records GROUP BY source,disposition")],
              "human_audit_performed": False, "policy_rollouts_performed": False,
              "notes": ["Evaluation assets are source-row references, not copied labels for new policy answers.",
                        "No manual review, factual-oracle gate, or policy-difficulty calibration was applied."]}
    task_counts = Counter()
    for r in selected:
        label = json.loads(db.execute("SELECT label FROM calls WHERE gid=? AND status='complete'", (r["gid"],)).fetchone()[0])
        task_counts[label["task_type"]] += 1
    report["selected_by_task_type"] = dict(task_counts)
    usage = Counter()
    for call in db.execute("SELECT response_path FROM calls"):
        if not call["response_path"]:
            usage["unknown_usage_calls"] += 1
            continue
        response = json.loads((run / call["response_path"]).read_text())
        normalized = response.get("normalized") or {}
        if response.get("dispatched") is False:
            usage["undispatched_attempts"] += 1
            continue
        if not normalized.get("usage_known"):
            usage["unknown_usage_calls"] += 1
        else:
            usage["input_tokens"] += normalized["prompt_tokens"]
            usage["output_tokens"] += normalized["completion_tokens"]
            if normalized.get("cached_tokens") is not None:
                usage["cached_input_tokens"] += normalized["cached_tokens"]
            usage["known_usage_calls"] += 1
    report["usage"] = dict(usage)
    write_json(run / "selection_report.json", report)
    state["phase"] = report["status"]
    write_json(run / "manifest.json", state)
    db.close()
    return report
