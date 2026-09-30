"""Frozen-artifact scoring/audit with the same durable model and execution path.

Evidence goes to a new run. Nothing is fed back to a synthesis run or its exports.
"""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..driver import RunDriver
from ..inputs import InputItem, record_digest
from ..schemas import InputLocation, SuiteDraft
from ..storage.canonical import canonical_json
from ..storage.blobs import atomic_write_bytes
from .suite import admit_suite


def new_run_dir(task, *, parent=None):
    parent = Path(parent) if parent else Path(__file__).resolve().parents[3] / 'runs'
    stem = datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y_%m_%d_%H_%M_') + task
    candidate, index = parent / stem, 2
    while candidate.exists():
        candidate = parent / (stem + str(index))
        index += 1
    return candidate


def evaluate_frozen(config, queries, definitions, source, *, audit=False, allow_prototype=False, run_dir=None):
    from .taskpack import whitelist_example
    cfg = config.model_copy(deep=True)
    cfg.data.split = 'audit' if audit else 'evaluation'
    cfg.construction.reuse_enabled = False
    cfg.budget.absolute_deadline_utc = None
    if cfg.budget.wall_deadline_s is None:
        cfg.budget.wall_deadline_s = 300
    root = Path(run_dir) if run_dir else new_run_dir('HarnessAudit' if audit else 'HarnessScore')
    rows = []
    source = Path(source)
    data = json.loads(source.read_text()) if audit else [json.loads(l) for l in source.read_text().splitlines() if l.strip()]
    requested = set(data) if audit else {c['query_id'] for c in data}
    if not requested <= definitions.keys():
        raise ValueError('evaluation source references queries without frozen successful rewards')
    with RunDriver(root, config=cfg, input_path=source) as driver:
        if audit and driver.runner.capabilities().max_assurance != 'isolated' and not allow_prototype:
            raise ValueError('formal audit requires an isolated runner; pass --allow-prototype-audit for explicit prototype evidence')
        # Each query owns one ledger; examples are deduplicated by suite during audit.
        for index, (qid, (definition, construction)) in enumerate(definitions.items(), 1):
            query = queries[qid]
            pack = driver.packs.get(query.task_profile_id).model_copy(deep=True)
            item = InputItem(InputLocation(path=str(source.resolve()), line=index), query, None, record_digest(query))
            state, budget, dispatcher, llm = driver._episode(item, pack)
            evaluator = dispatcher.evaluator
            try:
                if audit:
                    if qid not in data:
                        raise ValueError('audit mapping must contain a full independent SuiteDraft for every query')
                    pack.suite_policy.categories = ['fail', 'pass', 'ranking']
                    draft = SuiteDraft.model_validate(data[qid])
                    suite = admit_suite(draft, query, pack, generation_config_ref='external_audit', blobs=driver.blobs)
                    evaluator.namespace = 'audit'
                    budget.debit_once(state['episode_id'] + ':cases', {'test_cases': float(len(suite.cases))})
                    outcome = evaluator.validate(definition, pack, suite, reward_key=construction.reward_key, action_id=state['episode_id'] + ':audit')
                    row = {'query_id': qid, 'construction_done': True, **outcome.report.model_dump(mode='json')}
                    rows.append(row)
                    driver.journal.append('audit_result', {'report_ref': driver.blobs.put_json(row)}, episode_id=state['episode_id'])
                else:
                    candidates = [c for c in data if c['query_id'] == qid]
                    for number, candidate in enumerate(candidates):
                        example = whitelist_example({'query': query.query, 'reference': query.reference,
                            'metadata': query.metadata, **query.metadata, 'response': candidate['response']}, pack.permitted_inputs)
                        scores = evaluator.durable_scores(definition, pack, [example], [str(number)],
                            action_id=f"{state['episode_id']}:score:{number}", reward_key=construction.reward_key)
                        row = {'query_id': qid, 'candidate_id': candidate.get('candidate_id'), **scores[0].model_dump(mode='json')}
                        rows.append(row)
                        driver.journal.append('candidate_score', {'score_ref': driver.blobs.put_json(row)}, episode_id=state['episode_id'])
                driver.run_ledger = budget.run
            finally:
                for client in dispatcher.role_clients.values():
                    client.adapter.close()
        driver.journal.append('run_completed', {'state': 'COMPLETED', 'purpose': 'audit' if audit else 'score', 'records': len(rows)})
    atomic_write_bytes(root / 'evaluation.json', canonical_json({'rows': rows, 'training_target_eligible': False}))
    return rows, str(root.resolve())
