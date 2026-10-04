import argparse
import asyncio
import json
from pathlib import Path

from ..storage.lock import RunDirLock
from .config import load_config
from .export import export_runs
from .sampler import replay, sample
from .store import prepare, read, summarize, write


def default_project():
    return next((p for p in Path.cwd().resolve().parents if (p / "codebase/reward_harness/pyproject.toml").exists()),
                next((p for p in Path(__file__).resolve().parents if (p / "codebase/reward_harness/pyproject.toml").exists()), Path.cwd()))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Resumable policy rollouts and multi-model exports")
    parser.add_argument("--project-root", type=Path, default=default_project())
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Freeze config and dataset; no network calls")
    p.add_argument("--config", type=Path, required=True)
    p.add_argument("--task", default="QwenNineBRollout")
    p.add_argument("--limit", type=int, help="Seeded subset for a separate pilot run")
    for name in ("sample", "status", "replay", "export"):
        p = sub.add_parser(name)
        p.add_argument("--run", type=Path, required=True)
        if name == "sample":
            p.add_argument("--max-new-calls", type=int, help="Cap new physical calls for this invocation")
            p.add_argument("--retry-uncertain", action="store_true", help="Explicitly accept possible duplicate remote generation/cost")
            p.add_argument("--retry-failed", action="store_true", help="Retry failed calls within frozen attempt/request budgets")
        if name == "export":
            p.add_argument("--include-truncated", action="store_true")
            p.add_argument("--parquet", action="store_true")
            p.add_argument("--bundle-evidence", action="store_true")
    p = sub.add_parser("merge", help="Join models/runs on the same frozen prompt dataset")
    p.add_argument("--runs", nargs="+", type=Path, required=True)
    p.add_argument("--include-truncated", action="store_true")
    p.add_argument("--parquet", action="store_true")
    p.add_argument("--bundle-evidence", action="store_true")
    sub.add_parser("catalog", help="List policy runs and merges without a mutable central registry")
    args = parser.parse_args(argv)
    project = args.project_root.resolve()
    parent = project / "codebase/reward_harness/runs"
    for run in ([args.run] if hasattr(args, "run") else getattr(args, "runs", [])):
        if run.resolve().parent != parent or not (run / "manifest.json").is_file():
            parser.error("Run must already exist directly under codebase/reward_harness/runs")
    if args.command == "prepare":
        run = prepare(project, load_config(args.config), args.task, args.limit)
        result = {"run": str(run), "stage": "prepared", **summarize(run)}
    elif args.command == "sample":
        result = asyncio.run(sample(args.run.resolve(), max_new_calls=args.max_new_calls,
                                    retry_uncertain=args.retry_uncertain, retry_failed=args.retry_failed))
    elif args.command == "status":
        result = summarize(args.run)
        result["unfinished_attempt_note"] = "Uncommitted attempts may still be in flight if a sampler is running; otherwise they are uncertain."
    elif args.command == "replay":
        with RunDirLock(args.run):
            result = replay(args.run.resolve())
            write(args.run / "replay.json", result)
    elif args.command in ("export", "merge"):
        result = export_runs(args.runs if args.command == "merge" else [args.run], project=project,
                             merge=args.command == "merge", include_truncated=args.include_truncated,
                             parquet=args.parquet, bundle=args.bundle_evidence)
    else:
        result = []
        for path in sorted(parent.glob("*/manifest.json")):
            manifest = read(path)
            if manifest.get("kind") in ("policy_rollout", "rollout_merge"):
                result.append({"run": str(path.parent), "kind": manifest["kind"], "model": manifest.get("model"),
                               "created_at": manifest["created_at"], "planned_rollouts": manifest.get("planned_rollouts")})
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if isinstance(result, dict) and (result.get("passed") is False or result.get("stop_reason") in
                                    ("service_contract_failure", "request_budget_exhausted",
                                     "uncertain_request_requires_review", "service_retries_exhausted")):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
