"""Typed construction tools, durable action results and one trusted finalizer."""
from __future__ import annotations

import ast
import json
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import Field, ValidationError

from ..durability import fault, watchdog
from ..errors import ConfigError, HarnessError, StorageError
from ..schemas import RewardDefinition, Strict, StructuredError, ToolResult, ValidationReport
from ..storage.canonical import digest, reward_key_for
from .resources import Resource


class ReadArgs(Strict):
    resource_id: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4000, ge=1, le=16000)


class TestArgs(Strict):
    definition: RewardDefinition
    dev_profile_id: str
    display_limit: int = Field(default=3, ge=0, le=20)
    on_pass: Literal['submit', 'return'] = 'submit'


class SubmitArgs(Strict):
    reward_key: str
    validation_run_id: str


ARGUMENTS = {'read_resource': ReadArgs, 'test_reward': TestArgs, 'submit_reward': SubmitArgs}
TOOL_SCHEMAS = [{'name': k, 'parameters': v.model_json_schema()} for k, v in ARGUMENTS.items()]


def validate_batch(actions):
    # Validate the ENTIRE batch before dispatching any member.
    return [ARGUMENTS[a.tool].model_validate(a.arguments) for a in actions]


class ToolDispatcher:
    def __init__(self, *, episode_id, config, pack, suite, evaluator, resources, budget, journal, blobs, deadline):
        self.episode_id, self.config, self.pack, self.suite = episode_id, config, pack, suite
        self.evaluator, self.resources, self.budget = evaluator, resources, budget
        self.journal, self.blobs, self.deadline = journal, blobs, deadline
        self.validations = {}
        self.restore()

    def restore(self):
        for e in self.journal.scan().events:
            if e.episode_id == self.episode_id and e.type == 'tool_result':
                r = ToolResult.model_validate(self.blobs.get_json(e.payload['result_ref']))
                self._register(r)

    def _register(self, result):
        r = result.result or {}
        if 'validation_ref' in r:
            report = ValidationReport.model_validate(self.blobs.get_json(r['validation_ref']))
            self.validations[report.report_id] = (r['definition_ref'], r['validation_ref'])
            self.resources.add(Resource('report:' + report.report_id, 'json', 'Development report',
                lambda ref=r['validation_ref']: self.feedback_view(self.blobs.get_json(ref))))

    def feedback_view(self, value):
        if self.config.synthesis.feedback == 'full':
            return value
        # Apply the same projection to observations AND read_resource reports.
        hidden = {'feedback', 'evidence', 'raw_value', 'rationale', 'repair_feedback', 'message',
                  'detail_ref', 'suggested_recovery', 'error_categories'}
        if isinstance(value, dict):
            return {k: self.feedback_view(v) for k, v in value.items() if k not in hidden}
        if isinstance(value, list):
            return [self.feedback_view(v) for v in value]
        return value

    def emit(self, kind, **payload):
        return self.journal.append(kind, payload, episode_id=self.episode_id)

    def batch(self, actions, logical):
        args = validate_batch(actions)
        # Read-only resources are independent; executor.map is an ordered barrier.
        # Behavioral tests are sequential to avoid overlapping local watchdogs.
        if all(a.tool == 'read_resource' for a in actions):
            with ThreadPoolExecutor(max_workers=self.config.execution.max_concurrent_actions) as pool:
                results = list(pool.map(lambda pair: self.execute(pair[0], pair[1], logical), zip(actions, args)))
        else:
            results = [self.execute(a, arg, logical) for a, arg in zip(actions, args)]
        self.emit('batch_barrier', logical_call_id=logical, order=[a.id for a in actions])
        return results

    def execute(self, action, args, logical):
        aid = f'{logical}:{action.id}'
        events = [e for e in self.journal.scan().events if e.episode_id == self.episode_id
                  and e.payload.get('action_id') == aid]
        stored = next((e for e in events if e.type == 'tool_result'), None)
        if stored:
            result = ToolResult.model_validate(self.blobs.get_json(stored.payload['result_ref']))
            self._register(result)
            return result
        self.deadline.check('tool admission')
        self.budget.debit_once(aid, {'tool_calls': 1.0})
        if not any(e.type == 'tool_intent' for e in events):
            self.emit('tool_intent', action_id=aid, logical_call_id=logical, tool=action.tool,
                arguments_ref=self.blobs.put_json(args.model_dump(mode='json')), safe_to_replay=True)
        fault('tool_before_dispatch')
        self.emit('tool_dispatched', action_id=aid, logical_call_id=logical)
        fault('tool_after_dispatch')
        try:
            if action.tool == 'read_resource':
                r = self.resources.get(args.resource_id)
                if r is None:
                    raise ValueError('resource is not in the frozen episode index')
                content = r.loader()
                text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
                limit = min(args.limit, self.config.logging.max_observation_chars)
                result = {'resource_id': args.resource_id, 'content': text[args.offset:args.offset + limit],
                          'content_digest': digest(content), 'truncated': len(text) > args.offset + limit}
            elif action.tool == 'test_reward':
                if 'on_pass' not in action.arguments:
                    args = args.model_copy(update={'on_pass': self.config.construction.on_pass})
                result = self.test_reward(args, aid)
            else:
                if args.validation_run_id not in self.validations:
                    raise ValueError('validation run is not owned by this episode')
                dref, vref = self.validations[args.validation_run_id]
                definition, report = self.finalize(dref, vref)
                if reward_key_for(definition) != args.reward_key:
                    raise ValueError('report_mismatch: selected key differs from tested definition')
                result = {'accepted': True, 'definition_ref': dref, 'validation_ref': vref,
                          'reward_key': args.reward_key, 'eligible': True, 'on_pass': 'submit'}
            output = ToolResult(call_id=action.id, tool=action.tool, status='completed', result=result)
        except (ValueError, SyntaxError) as exc:
            output = ToolResult(call_id=action.id, tool=action.tool, status='failed',
                error=StructuredError(category='protocol', code='invalid_tool_arguments', phase='tool',
                    retry_owner='agent', action_id=aid, message=str(exc)))
        ref = self.blobs.put_json(output.model_dump(mode='json'))
        self.emit('tool_result', action_id=aid, logical_call_id=logical, result_ref=ref)
        fault('tool_after_result')
        self._register(output)
        return output

    def check_definition(self, definition):
        from ..runtime.policy import SELF_CONTAINED, validate_component
        from ..runtime.errors import ForbiddenAPI
        if definition.runtime_contract.reward_logic_policy != SELF_CONTAINED:
            raise ValueError('runtime_contract.reward_logic_policy must be self_contained_v1')
        for component in definition.components:
            try:
                validate_component(component, dependencies=definition.runtime_contract.dependencies)
            except SyntaxError:
                pass  # Syntax errors remain explicit component execution failures.
            except ForbiddenAPI as exc:
                raise ValueError(str(exc)) from exc
        if definition.schema_version != 'rlar.reward.v2' or self.suite is None:
            raise ValueError('v2 requires a frozen suite and v2 reward')
        from ..evaluation.suite import assert_frozen
        assert_frozen(self.suite)
        # Reward may group capabilities differently, but may not alter task meaning.
        if definition.capabilities != self.pack.capabilities:
            raise ValueError('reward capability descriptions differ from the fixed task')
        for component in definition.components:
            if component.judge_spec:
                ref = self.config.roles['rubric_judge']
                if component.judge_spec.model_ref != ref or component.judge_spec.model_config_digest != digest(self.config.models[ref]):
                    raise ValueError('rubric judge model/config not frozen to the run catalogue')
                tree = ast.parse(component.source)
                if component.judge_spec.prompt_template in [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)]:
                    raise ValueError('rubric template has one authority: context.judge_spec')
                if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == component.judge_spec.parser_entrypoint for n in tree.body):
                    raise ValueError('rubric parser entrypoint is missing')
        if definition.mode not in self.pack.mode_constraints:
            raise ValueError('mode not permitted by profile')
        if len(definition.components) > min(self.pack.max_components, self.config.construction.max_components):
            raise ValueError('component limit exceeded')
        if definition.runtime_contract.environment_ref != self.config.execution.runtime_fingerprint:
            raise ValueError(
                'runtime fingerprint does not match the frozen run: '
                f'runtime_contract.environment_ref must be {self.config.execution.runtime_fingerprint!r}; '
                f'got {definition.runtime_contract.environment_ref!r}. '
                'Set python_version to 3.11+ or the exact Python revision, not to the environment fingerprint.'
            )
        import sys
        declared_python = definition.runtime_contract.python_version
        if declared_python != '3.11+' and declared_python != '.'.join(map(str, sys.version_info[:3])):
            raise ConfigError('pinned Python revision does not match the runner')
        if definition.runtime_contract.aggregator_version != 'agg.v1':
            raise ValueError('unsupported aggregation version')
        for component in definition.components:
            if not set(component.required_apis) <= set(self.pack.permitted_apis):
                raise ValueError('required API is not permitted')
            mappings = self.pack.normalization_mappings
            if component.normalization.kind == 'mapping' and component.normalization.mapping_id not in mappings:
                raise ValueError('normalization mapping was not declared')
            # Conservative static screen for obvious per-query/sample lookups.
            # Syntax errors remain independent runtime component errors.
            try:
                tree = ast.parse(component.source)
            except SyntaxError:
                continue
            forbidden = {e.id for e in self.suite.examples} | {c.id for c in self.suite.cases}
            forbidden |= {e.response for e in self.suite.examples if isinstance(e.response, str) and len(e.response) >= 3}
            if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in forbidden for n in ast.walk(tree)):
                raise ValueError('frozen candidate answer/ID lookup is not a reusable reward')
            if any(isinstance(n, ast.Constant) and n.value in ('query_id', 'case_id', 'is_correct', 'expected_label', 'suite_digest', 'admission_report_ref')
                   for n in ast.walk(tree)):
                raise ValueError('sample ID/oracle lookup is not a reusable reward')

    def test_reward(self, args, aid):
        if args.dev_profile_id != self.pack.profile_id:
            raise ValueError('only the current development profile may be tested')
        self.check_definition(args.definition)
        definition = args.definition
        key = reward_key_for(definition)
        dref = self.blobs.put_json(definition.model_dump(mode='json'))
        report = None
        for event in self.journal.scan().events:
            if event.episode_id != self.episode_id or event.type != 'evaluation_result':
                continue
            prior = ValidationReport.model_validate(self.blobs.get_json(event.payload['validation_ref']))
            if (prior.reward_key == key and prior.suite_digest == self.suite.suite_digest
                and prior.verifier_config_digest == self.evaluator.verifier.config_digest
                and self.evaluator.verify(prior)):
                report = prior
                break
        if report is None:
            eid = f'{aid}:evaluation:0'
            self.budget.debit_once(eid, {'test_cases': float(len(self.suite.cases))})
            self.emit('evaluation_dispatched', evaluation_id=eid, reward_key=key, definition_ref=dref)
            with watchdog(self.deadline.bounded(self.config.budget.per_tool_timeout_s)):
                outcome = self.evaluator.validate(definition, self.pack, self.suite, reward_key=key, action_id=eid)
            report = outcome.report
            codes = {c.error.code for r in report.per_case for c in r.component_results if c.error}
            if 'auth_failed' in codes:
                raise HarnessError('judge authentication failed', code='auth_failed')
            if 'scoring_budget_exhausted' in codes:
                from ..errors import BudgetExhausted
                raise BudgetExhausted('shared scoring budget exhausted')
            vref = self.blobs.put_json(report.model_dump(mode='json'))
            self.emit('evaluation_result', evaluation_id=eid, validation_ref=vref,
                      infrastructure_failures=outcome.infrastructure_failures)
        vref = self.blobs.put_json(report.model_dump(mode='json'))
        from ..evaluation.evaluator import INFRASTRUCTURE_CODES
        for case, decision in zip(self.suite.cases, report.decisions):
            if not case.required or decision.operation_status == 'completed':
                continue
            relevant = {c.id for c in definition.components if case.scope == 'overall' or set(c.capability_ids) & set(case.capability_ids)}
            if any(c.id in relevant and c.error and c.error.code in INFRASTRUCTURE_CODES
                   for score in report.per_case if score.example_id in case.example_ids for c in score.component_results):
                raise HarnessError('required scoring service unavailable', code='environment_unavailable')
        return {'reward_key': key, 'definition_ref': dref, 'validation_ref': vref,
                'validation_run_id': report.report_id, 'eligible': report.eligible,
                'assurance': report.assurance, 'on_pass': args.on_pass,
                'metrics': report.metrics.model_dump(mode='json'), 'reasons': report.ineligibility_reasons,
                'displayed_cases': self.feedback_view([r.model_dump(mode='json') for r in report.per_case[:args.display_limit]]),
                'complete_case_count': len(report.per_case),
                'verification_unavailable': any(d.operation_status == 'error' for d in report.decisions if any(c.id == d.case_id and c.required for c in self.suite.cases)),
                'insufficient_evidence': any(d.operation_status == 'insufficient_evidence' for d in report.decisions if any(c.id == d.case_id and c.required for c in self.suite.cases)),
                'decisions': self.feedback_view([d.model_dump(mode='json') for d in report.decisions])}

    def finalize(self, dref, vref):
        definition = RewardDefinition.model_validate(self.blobs.get_json(dref))
        report = ValidationReport.model_validate(self.blobs.get_json(vref))
        policy = self.pack.acceptance_policy
        if not self.evaluator.verify(report) or not report.eligible:
            raise ValueError('report_not_trusted_or_eligible')
        if (reward_key_for(definition) != report.reward_key or report.task_contract_digest != self.pack.applicability_rule.task_contract_digest or report.runtime_fingerprint != self.pack.applicability_rule.runtime_fingerprint or (report.profile_id != self.pack.profile_id) or (report.verifier_version != self.pack.verifier_version) or (report.policy != policy) or (self.suite is None) or (report.suite_version != self.suite.version) or (report.suite_id != self.suite.suite_id) or (report.suite_digest != (self.suite.suite_digest))):
            raise ValueError('report_mismatch: artifact, contract, suite, policy or runtime changed')
        self.check_definition(definition)
        from ..evaluation.verifier import evidence_for, check_decision
        if report.schema_version != 'rlar.validation.v2' or report.verifier_config_digest != self.evaluator.verifier.config_digest:
            raise ValueError('verifier configuration changed')
        if report.evidence_digest != digest(report.per_case) or len(report.decisions) != len(self.suite.cases):
            raise ValueError('missing or changed execution/decision evidence')
        for case, decision in zip(self.suite.cases, report.decisions):
            evidence = evidence_for(case, self.suite, definition, report.per_case, self.pack, self.evaluator.verifier, query=self.evaluator.query)
            check_decision(decision, evidence)
            if case.required and (decision.operation_status != 'completed' or decision.passed is not True):
                raise ValueError('required case is not passed')
        return definition, report

    def select(self, results, *, all_eligible=False):
        candidates = []
        for result in results:
            r = result.result or {}
            if r.get('eligible') and (all_eligible or r.get('on_pass') == 'submit'):
                d, report = self.finalize(r['definition_ref'], r['validation_ref'])
                metric = getattr(report.metrics, self.config.construction.selector_metric, None)
                higher = self.config.construction.selector_metric in ('ranking_accuracy', 'coverage')
                rank = (-metric if higher else metric) if metric is not None else float('inf')
                candidates.append((rank, reward_key_for(d), r))
        return min(candidates, key=lambda x: (x[0], x[1]))[2] if candidates else None
