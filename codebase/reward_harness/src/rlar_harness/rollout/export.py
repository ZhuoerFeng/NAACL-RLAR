"""Portable normalized rows and prompt-grouped multi-model candidate pools."""
import tarfile
import uuid
from collections import Counter
from contextlib import ExitStack
from pathlib import Path

from ..storage.lock import RunDirLock
from .sampler import replay
from .store import attempts, digest, dumps, latest, load_run, new_run, now, tasks, write, write_bytes


def collect(run):
    manifest, cfg, prompts = load_run(run)
    integrity = replay(run)
    if not integrity["passed"]:
        raise ValueError(f"Replay failed for {run}; refusing export")
    rows = []
    for task in tasks(manifest, cfg, prompts):
        result = latest(run, task)
        call_paths = attempts(run, task)
        row = {
            "schema_version": "rlar.rollout.v1", "rollout_id": task["rollout_id"],
            "run_id": manifest["run_id"], "dataset_version": manifest["dataset"]["version"],
            "dataset_sha256": manifest["dataset"]["sha256"],
            "sample_id": task["sample_id"], "sample_index": task["sample_index"],
            "data_source": task["data_source"], "source_line": task["source_line"],
            "prompt": task["prompt"], "prompt_sha256": task["prompt_sha256"],
            "model": cfg.model, "model_revision": cfg.model_revision,
            "generation": cfg.generation_parameters(task["sample_index"]), "thinking": cfg.thinking,
            "config_sha256": manifest["config_sha256"], "status": result["status"],
            "evidence_kind": result.get("evidence_kind", "uncommitted" if call_paths else "not_executed"),
            "response": result.get("content", ""), "reasoning_content": result.get("reasoning_content", ""),
            "refusal": result.get("refusal", ""), "finish_reason": result.get("finish_reason"),
            "usage": result.get("usage"), "usage_complete": result.get("usage_complete", False),
            "returned_models": result.get("returned_models", []),
            "http_status": result.get("http_status"), "elapsed_seconds": result.get("elapsed_seconds"),
            "attempt_count": len(call_paths), "source_run": str(Path(run).resolve()),
            "evidence_path": str(call_paths[-1].relative_to(run)) if call_paths else None,
            "request_sha256": result.get("request_sha256"), "response_sha256": result.get("response_sha256"),
        }
        rows.append(row)
    return manifest, rows, integrity


def export_runs(runs, *, project=None, merge=False, include_truncated=False, parquet=False, bundle=False):
    runs = sorted({Path(p).resolve() for p in runs})
    if not runs or (not merge and len(runs) != 1):
        raise ValueError("export needs one source; merge needs one or more sources")
    if parquet:
        import pyarrow as pa
        import pyarrow.parquet as pq
    with ExitStack() as stack:
        for run in runs:
            stack.enter_context(RunDirLock(run))
        collected = [collect(run) for run in runs]
        datasets = {(m["dataset"]["version"], m["dataset"]["sha256"]) for m, _, _ in collected}
        if len(datasets) != 1:
            raise ValueError("Cannot merge different dataset versions or prompt-file fingerprints")
        rows, seen, prompt_map = [], {}, {}
        duplicates = 0
        for _, source_rows, _ in collected:
            for row in source_rows:
                key = row["rollout_id"]
                comparable = {k: v for k, v in row.items() if k != "source_run"}
                if key in seen:
                    if comparable != seen[key]:
                        raise ValueError("Conflicting copies of the same rollout_id")
                    duplicates += 1
                    continue
                seen[key] = comparable
                sid = row["sample_id"]
                identity = {k: row[k] for k in ("sample_id", "data_source", "prompt", "prompt_sha256", "source_line")}
                if sid in prompt_map and prompt_map[sid] != identity:
                    raise ValueError("Conflicting prompts for the same sample_id")
                prompt_map[sid] = identity
                rows.append(row)
        rows.sort(key=lambda row: (row["source_line"], row["model"], row["run_id"], row["sample_index"]))
        accepted = {"success", "truncated"} if include_truncated else {"success"}
        candidates = [r for r in rows if r["status"] in accepted and r["response"].strip()]
        grouped = {sid: {**value, "rollouts": []} for sid, value in prompt_map.items()}
        for row in candidates:
            grouped[row["sample_id"]]["rollouts"].append({k: v for k, v in row.items()
                                                         if k not in ("prompt", "data_source", "source_line", "sample_id")})
        by_prompt = sorted(grouped.values(), key=lambda row: row["source_line"])
        source_info = [{"path": str(run), "run_id": m["run_id"], "model": m["model"],
                        "manifest_sha256": digest((run / "manifest.json").read_bytes()),
                        "config_sha256": m["config_sha256"], "replay": integrity}
                       for run, (m, _, integrity) in zip(runs, collected)]
        target = new_run(project, "RolloutMerge") if merge else runs[0]
        export_id = uuid.uuid4().hex
        output = target / "exports" / export_id
        output.mkdir(parents=True)
        files = {}
        for name, values in (("rollouts", rows), ("candidates", candidates), ("by_prompt", by_prompt)):
            path = output / f"{name}.jsonl"
            write_bytes(path, "".join(dumps(row) + "\n" for row in values).encode())
            files[path.name] = {"sha256": digest(path.read_bytes()), "rows": len(values)}
            if parquet and values:
                pq.write_table(pa.Table.from_pylist(values), output / f"{name}.parquet")
                files[f"{name}.parquet"] = {"sha256": digest((output / f"{name}.parquet").read_bytes()), "rows": len(values)}
        report = {"schema_version": "rlar.rollout.export.v1", "export_id": export_id, "created_at": now(),
                  "dataset": collected[0][0]["dataset"], "sources": source_info, "files": files,
                  "status_counts": dict(Counter(r["status"] for r in rows)),
                  "candidate_counts_by_model": dict(Counter(r["model"] for r in candidates)),
                  "include_truncated": include_truncated, "duplicate_rollouts_removed": duplicates,
                  "prompts_without_candidates": sum(not g["rollouts"] for g in by_prompt),
                  "note": "No gold answers, reward labels, or automatic preference pairs. Different runs remain separate candidates."}
        write(output / "export.json", report)
        if merge:
            write(target / "manifest.json", {"schema_version": "rlar.rollout.merge.v1", "kind": "rollout_merge",
                                              "created_at": now(), "sources": source_info, "export_id": export_id})
        if bundle:
            # Include explicit input/evidence trees only, not credentials, locks, old exports, or this archive.
            archive = output / "evidence.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for run, (manifest, _, _) in zip(runs, collected):
                    for name in ("manifest.json", "config.json", "inputs", "calls"):
                        path = run / name
                        if path.exists():
                            tar.add(path, arcname=f"sources/{manifest['run_id']}/{name}")
                for name in [*files, "export.json"]:
                    tar.add(output / name, arcname=f"export/{name}")
            write(output / "bundle.json", {"file": archive.name, "sha256": digest(archive.read_bytes()),
                                           "note": "Evidence paths resolve via sources/<run_id>; original absolute paths are provenance only."})
        return {"run": str(target), "output": str(output), "rows": len(rows), "candidates": len(candidates),
                "prompts": len(by_prompt), "status_counts": report["status_counts"], "files": files}
