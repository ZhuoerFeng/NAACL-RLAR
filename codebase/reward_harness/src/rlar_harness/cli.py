"""CLI for construction and deterministic analysis of existing runs."""
from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from .config import load_config, preflight
from .driver import RunDriver, construct_file, resolve_config
from .errors import HarnessError, PreflightError
from .evaluation.audit import Auditor, AuditSuiteStore
from .evaluation.evaluator import ExecutionLimits, TrustedEvaluator
from .evaluation.taskpack import TaskPackStore, whitelist_example
from .inputs import iter_jsonl
from .runtime.runner import SubprocessRunner
from .schemas import RewardDefinition
from .storage.blobs import atomic_write_bytes
from .storage.canonical import canonical_json
from .trace.export import export_llm_calls, export_sft, llm_calls, read_run, replay, _write_jsonl, guard_output
from .trace.report import report


def score_reward(definition, example, scoring_context, runner):
    from .storage.canonical import reward_key_for
    evaluator = TrustedEvaluator(runner)
    pack = scoring_context
    results, usage = evaluator.score_examples(definition, pack, [whitelist_example(example, pack.permitted_inputs)],
        ['candidate'], action_id='score', reward_key=reward_key_for(definition))
    result = results[0]
    result.usage = usage
    return result


def frozen_definitions(root):
    blobs, events, results, manifest = read_run(root)
    library = {}
    path = Path(root) / 'reward_library.jsonl'
    if path.exists():
        for line in path.read_text().splitlines():
            entry = json.loads(line)
            library[(entry['reward_key'], entry['validation_ref'])] = entry['definition']
    definitions = {}
    for result in results:
        if result.status != 'success':
            continue
        definition = result.reward_definition or RewardDefinition.model_validate(library[(result.reward_key, result.validation_ref)])
        from .storage.canonical import reward_key_for
        if reward_key_for(definition) != result.reward_key:
            raise ValueError("frozen artifact hash mismatch")
        definitions[result.query_id] = (definition, result)
    return definitions, manifest


def score_file(root, source, output):
    guard_output(root, output)
    if Path(source).resolve() == Path(output).resolve():
        raise ValueError("score output cannot overwrite candidate source")
    from .budget import BudgetLedger, SharedBudget
    from .runtime.broker import ScoringBroker
    definitions, manifest = frozen_definitions(root)
    from .config import HarnessConfig
    config = HarnessConfig.model_validate(manifest['config'])
    packs = TaskPackStore(Path(config.data.task_pack_root))
    queries = {x.record.query_id: x.record for x in iter_jsonl(Path(manifest['input_path'])) if x.ok}
    budget = SharedBudget(BudgetLedger('score', dict(config.budget.run)))
    runner = SubprocessRunner(scoring_service=ScoringBroker(config.rm, budget=budget))
    rows = []
    with open(source) as f:
        for line in f:
            candidate = json.loads(line)
            definition, result = definitions[candidate['query_id']]
            q = queries[candidate['query_id']]
            pack = packs.get(q.task_profile_id)
            example = {'query': q.query, 'reference': q.reference, **q.metadata, 'response': candidate['response']}
            reserved = budget.reserve({'component_executions': float(len(definition.components))})
            budget.settle(reserved)
            score = score_reward(definition, example, pack, runner)
            rows.append({'schema_version': 'rlar.candidate_score.v1', 'query_id': q.query_id,
                'candidate_id': candidate.get('candidate_id'), 'group_id': candidate.get('group_id'),
                **score.model_dump(mode='json')})
    _write_jsonl(output, rows)
    atomic_write_bytes(Path(str(output) + '.usage.json'), canonical_json(budget.run.to_json()))
    return {'scored': len(rows), 'output': str(output)}


def audit_run(root, suite, output, allow_prototype=False):
    guard_output(root, output)
    definitions, manifest = frozen_definitions(root)
    from .config import HarnessConfig
    config = HarnessConfig.model_validate(manifest['config'])
    runner = SubprocessRunner()
    limits = ExecutionLimits(**{k: getattr(config.execution, k) for k in ExecutionLimits.__dataclass_fields__})
    evaluator = TrustedEvaluator(runner, namespace='audit', limits=limits)
    auditor = Auditor(evaluator, AuditSuiteStore(config.validation.audit_suite_root))
    mapping = json.loads(Path(suite).read_text())
    packs = TaskPackStore(Path(config.data.task_pack_root))
    queries = {x.record.query_id: x.record for x in iter_jsonl(Path(manifest['input_path'])) if x.ok}
    entries = []
    seen = set()
    for qid, (definition, result) in definitions.items():
        profile = queries[qid].task_profile_id
        if (profile, result.reward_key) in seen:
            continue
        seen.add((profile, result.reward_key))
        entries.append((result.reward_key, definition, packs.get(profile), mapping[profile]))
    summary = auditor.audit(entries, allow_insufficient_assurance=allow_prototype)
    auditor.write_summary(summary, Path(output))
    return summary.to_json()


def build_parser():
    parser = argparse.ArgumentParser(prog='rlar-harness')
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('validate-config'); p.add_argument('--config', required=True)
    p = commands.add_parser('construct'); p.add_argument('--config', required=True); p.add_argument('--input'); p.add_argument('--run-dir', required=True)
    for name in ('resume', 'report', 'replay', 'export-llm-calls', 'export-sft', 'score', 'audit'):
        p = commands.add_parser(name); p.add_argument('--run-dir', required=True)
        if name in ('export-llm-calls', 'export-sft', 'score', 'audit'):
            p.add_argument('--output', required=True)
        elif name in ('report', 'replay'):
            p.add_argument('--output')
        if name == 'score': p.add_argument('--input', required=True)
        if name == 'audit':
            p.add_argument('--suite', required=True); p.add_argument('--allow-prototype-audit', action='store_true')
        if name == 'export-sft':
            p.add_argument('--format', choices=['per_call', 'full_trace', 'final_program_only'], default='per_call')
            p.add_argument('--max-tokens', type=int, default=131072)
        if name == 'replay':
            p.add_argument('--mode', choices=['observations', 'execute'], default='observations')
            p.add_argument('--execute-run-dir')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    def cancel(*_):
        raise KeyboardInterrupt('user cancelled')
    signal.signal(signal.SIGTERM, cancel)
    try:
        cmd = args.command
        if cmd in ('construct', 'validate-config'):
            config = resolve_config(load_config(args.config), Path(args.config).resolve().parent)
            if cmd == 'validate-config':
                result = preflight(config, Path(args.config).parent, runner_capabilities=SubprocessRunner().capabilities().model_dump())
                print(result.model_dump_json(indent=2))
                return 0 if result.ok else 2
            source = args.input or config.data.input_path
            if not source: raise ValueError('input path required')
            rows = construct_file(config, source, args.run_dir)
            result = {'results': len(rows), 'statuses': [r.status for r in rows], 'run_dir': str(Path(args.run_dir).resolve())}
        elif cmd == 'resume':
            rows = construct_file(None, None, args.run_dir, resume=True)
            result = {'new_results': len(rows), 'statuses': [r.status for r in rows]}
        elif cmd == 'report': result = report(args.run_dir)
        elif cmd == 'replay':
            if args.mode == 'execute':
                if not args.execute_run_dir or Path(args.execute_run_dir).resolve() == Path(args.run_dir).resolve():
                    raise ValueError('execute replay requires a separate --execute-run-dir')
                from .config import HarnessConfig
                _, _, _, m = read_run(args.run_dir)
                rows = construct_file(HarnessConfig.model_validate(m['config']), m['input_path'], args.execute_run_dir)
                result = {'mode': 'execute', 'new_run': args.execute_run_dir, 'results': len(rows)}
            else: result = replay(args.run_dir)
        elif cmd == 'export-llm-calls': result = export_llm_calls(args.run_dir, args.output)
        elif cmd == 'export-sft': result = export_sft(args.run_dir, args.output, format=args.format, max_tokens=args.max_tokens)
        elif cmd == 'score': result = score_file(args.run_dir, args.input, args.output)
        elif cmd == 'audit': result = audit_run(args.run_dir, args.suite, args.output, args.allow_prototype_audit)
        if cmd in ('report', 'replay') and args.output:
            guard_output(args.run_dir, args.output)
            atomic_write_bytes(Path(args.output), canonical_json(result))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (HarnessError, ValueError, OSError) as exc:
        print(json.dumps({'error': type(exc).__name__, 'code': getattr(exc, 'code', None), 'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('Interrupted; use explicit resume to continue the same run.', file=sys.stderr)
        return 130

if __name__ == '__main__':
    raise SystemExit(main())
