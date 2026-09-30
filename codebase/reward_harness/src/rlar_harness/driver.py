"""Single-writer streaming driver; results.jsonl is the only commit authority."""
from __future__ import annotations

import json
import os
import platform
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .budget import BudgetLedger, Deadline
from .config import HarnessConfig, RunManifest, preflight, require_preflight, resolve_paths
from .durability import PersistentBudget, fault, watchdog
from .episode import EpisodeContext, construct_one, initial_history, synthesize_tests
from .errors import ConfigError, HarnessError, StorageError
from .evaluation.audit import assert_not_controller_visible
from .evaluation.evaluator import ExecutionLimits, TrustedEvaluator
from .evaluation.taskpack import TaskPackStore
from .inputs import InputItem, file_digest, iter_jsonl, job_key, record_digest
from .llm.durable import DurableLLMClient
from .llm.http_chat_json import HttpChatJsonAdapter
from .llm.http_responses_json import HttpResponsesJsonAdapter
from .runtime.runner import SubprocessRunner
from .schemas import ConstructionResult, InputLocation, LibraryEntry, QueryRecord, RewardDefinition, StructuredError
from .storage.blobs import BlobStore, atomic_write_bytes
from .storage.canonical import canonical_json, digest, reward_key_for
from .storage.journal import Journal
from .storage.library import LibrarySnapshot, RewardLibrary
from .storage.lock import RunDirLock
from .storage.results import ResultsStore
from .tools.dispatcher import ToolDispatcher
from .tools.resources import build_resource_index


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def resource_fingerprints(config):
    files = []
    for root in (config.data.task_pack_root, config.validation.dev_suite_root):
        files.extend(Path(root).glob('*.json'))
    for model in config.models.values():
        if model.scripted_responses:
            files.append(Path(model.scripted_responses))
    files.extend(Path(__file__).parent.rglob('*.py'))
    result = {str(p.resolve()): file_digest(p) for p in sorted(files)}
    result['python'] = sys.version
    import importlib.metadata
    result['dependencies'] = digest({n: importlib.metadata.version(n) for n in ('pydantic', 'httpx', 'PyYAML')})
    return result


def resolve_config(config, base):
    c = config.model_copy(deep=True)
    paths = resolve_paths(c, Path(base))
    c.data.task_pack_root = str(paths['task_pack_root'])
    c.validation.dev_suite_root = str(paths['dev_suite_root'])
    c.validation.audit_suite_root = str(paths['audit_suite_root']) if paths['audit_suite_root'] else None
    if paths['input_path']:
        c.data.input_path = str(paths['input_path'])
    for model in c.models.values():
        if model.scripted_responses:
            model.scripted_responses = str((Path(base) / model.scripted_responses).resolve())
    return c


def adapter_for(config, record, role):
    model = config.role_model(role)
    if model.provider_adapter == 'http_chat_json_v1':
        return HttpChatJsonAdapter(model)
    if model.provider_adapter == 'http_responses_json_v1':
        return HttpResponsesJsonAdapter(model)
    from .llm.fixtures import fixture_adapter
    return fixture_adapter(config, record, role)


class RunDriver:
    def __init__(self, run_dir, *, config=None, input_path=None, resume=False, runner=None, llm_client=None):
        self.root = Path(run_dir).resolve()
        self.lock = RunDirLock(self.root)
        self.lock.acquire()
        try:
            self._initialize(config, input_path, resume, runner, llm_client)
        except BaseException:
            self.lock.release()
            raise

    def _initialize(self, config, input_path, resume, runner, llm_client):
        self.runner = runner or SubprocessRunner(memory_limit_mb=config.execution.memory_limit_mb if config else None)
        self.injected_llm = llm_client
        mpath = self.root / 'run_manifest.json'
        self.blobs = BlobStore(self.root / 'blobs')
        self.journal = Journal(self.root / 'trace.jsonl', self.blobs)
        if resume:
            if not mpath.exists():
                raise ConfigError('run manifest missing')
            from .config import require_current_manifest
            raw_manifest = json.loads(mpath.read_text())
            require_current_manifest(raw_manifest)
            self.manifest = RunManifest.model_validate(raw_manifest)
            config = self.manifest.config
            if config.digest() != self.manifest.config_digest:
                raise StorageError('manifest configuration digest mismatch')
            if self.manifest.input_path and file_digest(Path(self.manifest.input_path)) != self.manifest.input_digest:
                raise StorageError('input_changed: start a new run', code='input_changed')
            if resource_fingerprints(config) != self.manifest.resource_fingerprints:
                raise ConfigError('profile/suite/runtime dependency changed; start a new run')
            events = self.journal.scan(repair_tail=True).events
        else:
            if mpath.exists() or (self.root / 'trace.jsonl').exists():
                raise ConfigError('run exists; use resume or choose an empty run directory')
            if config is None:
                raise ConfigError('new run requires a config')
            require_preflight(preflight(config, self.root, runner_capabilities=self.runner.capabilities().model_dump()))
            if any(m.provider_adapter != 'scripted' for m in config.models.values()) and not config.execution.allow_untrusted_code:
                raise ConfigError('model-generated code requires explicit prototype allow_untrusted_code=true')
            deadline = config.budget.absolute_deadline_utc
            if deadline is None and config.budget.wall_deadline_s is not None:
                deadline = time.time() + config.budget.wall_deadline_s
            self.manifest = RunManifest(schema_version='rlar.manifest.v2', run_id=uuid.uuid4().hex, created_at=utc_now(), config=config,
                config_digest=config.digest(), input_path=str(Path(input_path).resolve()) if input_path else None,
                input_digest=file_digest(Path(input_path)) if input_path else None,
                input_record_count=sum(bool(line.strip()) for line in open(input_path, "rb")) if input_path else None,
                split=config.data.split, seed=config.construction.seed, deadline_utc=deadline,
                harness_version=__version__, platform=platform.platform(),
                runner_capabilities=self.runner.capabilities().model_dump(mode='json'), profile_kind=config.profile_kind,
                resource_fingerprints=resource_fingerprints(config),
                resolved_paths={k: str(v) if v else None for k, v in resolve_paths(config, self.root).items()})
            atomic_write_bytes(mpath, self.manifest.model_dump_json(indent=2).encode())
            events = []
        self.blobs.max_bytes = config.logging.max_blob_bytes
        self.config = config
        if isinstance(self.runner, SubprocessRunner):
            self.runner.work_root = self.root / 'workers'
            self.runner.work_root.mkdir(exist_ok=True)
        self.runner.memory_limit_mb = config.execution.memory_limit_mb if isinstance(self.runner, SubprocessRunner) else None
        self.deadline = Deadline(self.manifest.deadline_utc)
        secret_path = self.root / '.evaluator-secret'
        if not resume:
            fd = os.open(secret_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(secrets.token_bytes(32)); f.flush(); os.fsync(f.fileno())
        if not secret_path.exists():
            # Never silently mint another trust identity on resume.
            raise StorageError('evaluator integrity key missing')
        self.secret = secret_path.read_bytes()
        self.results = ResultsStore(self.root / 'results.jsonl')
        self.results.load(repair_tail=True)
        self.library = RewardLibrary(self.root / 'reward_library.jsonl', self.blobs)
        self._rebuild_library()
        for result in self.results.results:
            if result.validation_ref:
                self.blobs.get_json(result.validation_ref)
            if result.reward_key and not result.reward_definition and not self.library.get(result.reward_key):
                raise StorageError('committed reference definition missing')
            if result.reward_definition and reward_key_for(result.reward_definition) != result.reward_key:
                raise StorageError('committed definition hash mismatch')
        self.run_ledger = BudgetLedger('run', dict(config.budget.run))
        for e in events:
            if e.type == 'budget_snapshot':
                self.run_ledger = BudgetLedger.from_json(e.payload['run'])
        self.packs = TaskPackStore(Path(config.data.task_pack_root))
        self.journal.append('run_resumed' if resume else 'run_started', {'run_id': self.manifest.run_id,
                            'deadline_utc': self.manifest.deadline_utc, 'state': 'RUNNING'})

    def _rebuild_library(self):
        # Bind publication to the ACTUAL result at the sequence, including key,
        # validation ref and job identity. A later unrelated commit cannot publish
        # an orphan that happened to predict the same sequence number.
        path = self.library.path
        self.library._entries, self.library._by_key, self.library._unpublished = [], {}, []
        if not path.exists():
            return
        raw = path.read_bytes()
        if raw and not raw.endswith(b'\n'):
            raw = raw[:raw.rfind(b'\n') + 1]
            atomic_write_bytes(path, raw)
        for line in raw.splitlines():
            entry = LibraryEntry.model_validate_json(line)
            if reward_key_for(entry.definition) != entry.reward_key:
                raise StorageError('library definition hash mismatch')
            seq = entry.published_by_result_seq
            r = self.results.results[seq - 1] if 0 < seq <= len(self.results.results) else None
            published = r and r.status == 'success' and r.reward_key == entry.reward_key and r.validation_ref == entry.validation_ref and r.job_key == entry.provenance.get('job_key')
            if published:
                self.library._by_key[entry.reward_key] = entry
                if r.status == 'success' and not any(e.reward_key == entry.reward_key for e in self.library._entries):
                    self.library._entries.append(entry)
            else:
                self.library._unpublished.append(entry)

    def close(self):
        self.lock.release()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def process(self, records):
        try:
            committed = {r.input_location.as_key_suffix(): r for r in self.results.results}
            consecutive_environment_failures = 0
            pending_episodes = {e.episode_id for e in self.journal.scan().events if e.type == "episode_saved"}
            for item in records:
                if item.location.as_key_suffix() in committed:
                    prior = committed[item.location.as_key_suffix()]
                    if item.raw_digest != prior.input_digest:
                        raise StorageError('input position changed')
                    continue
                eid = digest({'run': self.manifest.run_id, 'position': item.location.as_key_suffix()})
                exhausted = [dim for dim in ('controller_steps', 'model_requests', 'tool_calls', 'test_cases', 'component_executions')
                             if self.run_ledger.remaining(dim) is not None and self.run_ledger.remaining(dim) <= 0]
                if exhausted and eid not in pending_episodes:
                    self.journal.append('run_interrupted', {'state': 'INTERRUPTED', 'reason': 'global_budget_exhausted',
                        'dimensions': exhausted})
                    return
                if not item.ok:
                    yield self._input_error(item, item.error)
                    continue
                try:
                    pack = self.packs.get(item.record.task_profile_id)
                except ConfigError as exc:
                    yield self._input_error(item, StructuredError(category='input', code='unknown_profile',
                        phase='input_validation', retry_owner='none', message=str(exc)))
                    continue
                mode = item.record.reward_mode or self.config.construction.reward_mode
                if (mode not in pack.mode_constraints or (item.record.reward_mode and not self.config.construction.allow_row_mode_override
                                                          and mode != self.config.construction.reward_mode)):
                    yield self._input_error(item, StructuredError(category='input', code='mode_not_permitted',
                        phase='input_validation', retry_owner='none', message='reward mode override is not permitted'))
                    continue
                state, budget, dispatcher, llm = self._episode(item, pack)
                context = EpisodeContext(state, self.config, dispatcher, llm, budget, self.journal, self.blobs, self.root, self.deadline)
                try:
                    synthesize_tests(item.record, context, dispatcher.role_clients['test_case_synthesizer'], dispatcher.snapshot)
                    if state['state'] == 'REUSE_CHECK' and state.get('suite_ref') and self.config.construction.reuse_enabled:
                        for entry in self.library.compatible_entries(dispatcher.snapshot, pack, mode):
                            dref = self.blobs.put_json(entry.definition.model_dump(mode='json'))
                            try:
                                dispatcher.finalize(dref, entry.validation_ref)
                            except ValueError:
                                continue
                            state.update(state='SELECTED', status='success', stop_reason='compatible_reuse', reused=True,
                                selected={'definition_ref': dref, 'validation_ref': entry.validation_ref, 'reward_key': entry.reward_key})
                            context.save()
                            break
                    final = construct_one(item.record, context)
                    result = self._commit(item, final, budget, dispatcher, pack)
                finally:
                    for client in dispatcher.role_clients.values():
                        client.adapter.close()
                self.run_ledger = budget.run
                yield result
                consecutive_environment_failures = (consecutive_environment_failures + 1
                    if result.stop_reason in ('environment_unavailable', 'infrastructure_error') else 0)
                if consecutive_environment_failures >= self.config.budget.retry.circuit_breaker_threshold:
                    self.journal.append('run_interrupted', {'state': 'INTERRUPTED', 'reason': 'environment_circuit_breaker',
                        'consecutive_environment_failures': consecutive_environment_failures})
                    return
                if self.deadline.expired():
                    self.journal.append('run_interrupted', {'state': 'INTERRUPTED', 'reason': 'deadline_exceeded'})
                    return
                exhausted = [dim for dim in ('controller_steps', 'model_requests', 'tool_calls', 'test_cases', 'component_executions')
                             if self.run_ledger.remaining(dim) is not None and self.run_ledger.remaining(dim) <= 0]
                if exhausted:
                    self.journal.append('run_interrupted', {'state': 'INTERRUPTED', 'reason': 'global_budget_exhausted',
                        'dimensions': exhausted})
                    return
            self.journal.append('run_completed', {'state': 'COMPLETED', 'results': len(self.results.results)})
        except BaseException as exc:
            # Never continue the input stream after a storage/auth/internal fault.
            # SIGKILL bypasses this entirely; journal/result recovery still works.
            self.journal.append('run_interrupted', {'state': 'INTERRUPTED', 'reason': getattr(exc, 'code', type(exc).__name__)})
            raise

    def _input_error(self, item, error):
        result = ConstructionResult(query_id=item.error_key(), input_location=item.location, input_digest=item.raw_digest,
            run_config_digest=self.config.digest(), library_snapshot=self.library.snapshot().snapshot_id,
            job_key=item.error_key(), episode_id=item.error_key(), attempt_id=1, status='failed', stop_reason=error.code,
            error=error, trace_ref='trace.jsonl')
        self.results.append(result)
        return result

    def _episode(self, item, pack):
        events = self.journal.scan().events
        eid = digest({'run': self.manifest.run_id, 'position': item.location.as_key_suffix()})
        saved = [e for e in events if e.episode_id == eid and e.type == 'episode_saved']
        if saved:
            state = self.blobs.get_json(saved[-1].payload['state_ref'])
            entries = [LibraryEntry.model_validate(e) for e in self.blobs.get_json(state['snapshot_ref'])]
            snapshot = LibrarySnapshot(state['library_snapshot'], tuple(entries))
        else:
            snapshot = self.library.snapshot() if self.config.construction.library_mode == 'continual' else LibrarySnapshot(digest({'count': 0, 'keys': []}), ())
            entries = tuple(e for e in snapshot.entries if e.definition.runtime_contract.reward_logic_policy == 'self_contained_v1')
            snapshot = LibrarySnapshot(digest({'count': len(entries), 'keys': [e.reward_key for e in entries]}), entries)
            if not self.config.construction.reuse_enabled:
                snapshot = LibrarySnapshot(digest({'count': 0, 'keys': []}), ())
            state = dict(episode_id=eid, query_id=item.record.query_id, state='REUSE_CHECK', step=0, started_at=time.time(),
                input_digest=item.raw_digest, library_snapshot=snapshot.snapshot_id,
                snapshot_ref=self.blobs.put_json([e.model_dump(mode='json') for e in snapshot.entries]),
                job_key=job_key(item.raw_digest, self.config.digest(), snapshot.snapshot_id), attempt_id=1,
                eligible=[], current=None, pending=[], history=[], selected=None)
        budget = PersistentBudget(self.run_ledger, BudgetLedger(eid, dict(self.config.budget.episode)), self.journal, eid, events)
        limits = ExecutionLimits(**{k: getattr(self.config.execution, k) for k in ExecutionLimits.__dataclass_fields__})
        evaluator = TrustedEvaluator(self.runner, limits=limits, integrity_secret=self.secret)
        cards = {ref: {'model': model.model, 'revision': model.revision, 'model_config_digest': digest(model)}
                 for ref, model in self.config.models.items()}
        resources = build_resource_index(item.record, pack, snapshot, model_cards=cards)
        assert_not_controller_visible({r.resource_id: r.loader() for r in resources.resources.values()},
            Path(self.config.validation.audit_suite_root) if self.config.validation.audit_suite_root else None)
        suite = None
        dispatcher = ToolDispatcher(episode_id=eid, config=self.config, pack=pack, suite=suite, evaluator=evaluator,
            resources=resources, budget=budget, journal=self.journal, blobs=self.blobs, deadline=self.deadline)
        from .evaluation.verifier import HarnessVerifier
        from .llm.utility import JudgeUtility
        clients = {}
        for role, ref in self.config.roles.items():
            model = self.config.models[ref]
            adapter = (self.injected_llm.get(role) if isinstance(self.injected_llm, dict) else self.injected_llm if role == 'reward_synthesizer' else None) or adapter_for(self.config, item.record, role)
            clients[role] = DurableLLMClient(adapter, model, budget=budget, journal=self.journal,
                blobs=self.blobs, episode_id=eid, run_id=self.manifest.run_id, task_id=item.record.query_id,
                split=self.manifest.split, actor_role=role, parent_job_id=state['job_key'],
                retry=self.config.budget.retry.model_copy(update={'max_transport_attempts': self.config.synthesis.model_transport_attempts}),
                deadline=self.deadline)
        dispatcher.role_clients, dispatcher.snapshot = clients, snapshot
        llm = clients['reward_synthesizer']
        verifier_ref = self.config.roles.get('harness_verifier', 'rule_baseline')
        evaluator.verifier = HarnessVerifier(self.config.synthesis, clients.get('harness_verifier'),
            model_ref=verifier_ref, model_config=self.config.models.get(verifier_ref))
        evaluator.persistence = (self.journal, self.blobs, eid)
        evaluator.budget = budget
        evaluator.execution_attempts = self.config.synthesis.model_transport_attempts
        evaluator.query = item.record
        if isinstance(self.runner, SubprocessRunner):
            self.runner.scoring_service = JudgeUtility(clients['rubric_judge'], self.config.roles['rubric_judge'])
        if not saved:
            EpisodeContext(state, self.config, dispatcher, llm, budget, self.journal, self.blobs, self.root, self.deadline).save()
        return state, budget, dispatcher, llm

    def _commit(self, item, state, budget, dispatcher, pack):
        selected = state.get('selected')
        definition = None
        report = None
        if selected:
            definition = RewardDefinition.model_validate(self.blobs.get_json(selected['definition_ref']))
            if state['status'] == 'success':
                definition, report = dispatcher.finalize(selected['definition_ref'], selected['validation_ref'])
        usage = budget.usage()
        usage.controller_logical_calls = 0
        usage.physical_requests = int(budget.episode.consumed['model_requests'] + budget.episode.reserved['model_requests'])
        usage.wall_seconds = max(0.0, time.time() - state.get('started_at', time.time()))
        attempts = [e for e in self.journal.scan().events if e.episode_id == state['episode_id'] and e.type == 'llm_dispatched']
        usage.transport_retries = len(attempts) - len({e.payload['logical_call_id'] for e in attempts})
        result = ConstructionResult(schema_version='rlar.result.v2', query_id=item.record.query_id, input_location=item.location,
            input_digest=item.raw_digest, run_config_digest=self.config.digest(), library_snapshot=state['library_snapshot'],
            job_key=state['job_key'], episode_id=state['episode_id'], attempt_id=1, status=state['status'],
            stop_reason=state['stop_reason'], reward_key=reward_key_for(definition) if definition else None,
            reward_definition=definition if self.config.data.output_layout == 'inline' else None,
            validation_ref=selected['validation_ref'] if selected else None, trace_ref='trace.jsonl',
            usage=usage, reused=state.get('reused', False))
        if definition:
            entry = LibraryEntry(reward_key=result.reward_key, definition=definition, applicability=pack.applicability_rule,
                validation_ref=result.validation_ref, validation_summary={k: getattr(report, k) for k in
                    ('eligible', 'verifier_version', 'suite_version', 'aggregation_version', 'profile_id')} if report else {},
                provenance={'job_key': state['job_key'], 'episode_id': state['episode_id']}, committed_at=utc_now(),
                published_by_result_seq=self.results.next_seq)
            self.library.append(entry)
        fault('after_library_before_result')
        self.results.append(result)
        fault('after_result_before_checkpoint')
        self._rebuild_library()
        self.journal.append('result_committed', {'job_key': result.job_key, 'result_seq': len(self.results.results)}, episode_id=result.episode_id)
        atomic_write_bytes(self.root / 'checkpoints/committed.json', canonical_json({'schema_version': 'rlar.checkpoint.v1',
            'cursor_line': item.location.line, 'committed_results': len(self.results.results)}))
        return result


def construct_file(config, input_path, run_dir, *, resume=False, runner=None, llm_client=None):
    with RunDriver(run_dir, config=config, input_path=input_path, resume=resume, runner=runner, llm_client=llm_client) as driver:
        path = Path(driver.manifest.input_path)
        return list(driver.process(iter_jsonl(path)))


def construct_stream(records, config, library=None, runner=None, llm_client=None, *, run_dir):
    """Streaming Python API. The caller supplies an isolated sidecar directory.

    This process owns the run-local library. For file resume use construct_file;
    in-memory streams must be provided again in their original order on resume.
    """
    if library is not None:
        raise ConfigError('pass a run-local library via run_dir; external library import is not supported')
    def items():
        seen = set()
        for index, value in enumerate(records, 1):
            record = QueryRecord.model_validate(value)
            if record.query_id in seen:
                error = StructuredError(category='input', code='duplicate_query_id', phase='input_validation', retry_owner='none')
            else:
                error = None
            seen.add(record.query_id)
            yield InputItem(InputLocation(path='memory://stream', line=index), record if not error else None, error, record_digest(record))
    with RunDriver(run_dir, config=config, runner=runner, llm_client=llm_client) as driver:
        yield from driver.process(items())
