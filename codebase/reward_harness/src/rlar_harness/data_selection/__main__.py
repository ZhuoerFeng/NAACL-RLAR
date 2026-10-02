import argparse
import json
from pathlib import Path

from .classifier import classify, load_env_file, replay_check
from .models import load_config
from .pipeline import export, fork_classifier, prepare, run_lock, status


def main():
    parser = argparse.ArgumentParser(description="RLAR training prompt selection; no policy rollouts")
    parser.add_argument("--project-root", type=Path, default=next(p for p in Path(__file__).resolve().parents if (p / "codebase/reward_harness/pyproject.toml").exists()))
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Index source splits and draw candidates; no model calls")
    p.add_argument("--config", required=True, type=Path)
    for name in ("classify", "export", "replay", "fork-classifier", "status"):
        p = sub.add_parser(name)
        p.add_argument("--run", required=True, type=Path)
        if name == "classify":
            p.add_argument("--env-file", type=Path)
            p.add_argument("--max-new-calls", type=int)
            p.add_argument("--requests-per-minute", type=int)
        if name == "fork-classifier":
            p.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    project = args.project_root.resolve()
    if args.command == "prepare":
        run = prepare(project, load_config(args.config))
        print(json.dumps({"run": str(run), "stage": "prepared"}, ensure_ascii=False))
        return
    run = args.run.resolve()
    if run.parent != project / "codebase/reward_harness/runs":
        parser.error("Run must be directly under this project's codebase/reward_harness/runs")
    if args.command == "status":
        print(json.dumps(status(run), ensure_ascii=False, indent=2))
        return
    with run_lock(run):
        if args.command == "fork-classifier":
            result = {"run": str(fork_classifier(run, load_config(args.config))), "stage": "prepared"}
        elif args.command == "classify":
            if args.max_new_calls is not None and args.max_new_calls < 1:
                parser.error("--max-new-calls must be positive")
            load_env_file(args.env_file)
            result = classify(run, args.max_new_calls, args.requests_per_minute)
        elif args.command == "export":
            result = export(run)
        else:
            result = replay_check(run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("stop_reason") in ("service_or_output_failure", "existing_service_failure_requires_reconciliation") or result.get("status") == "failed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
