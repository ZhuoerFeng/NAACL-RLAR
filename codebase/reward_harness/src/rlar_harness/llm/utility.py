"""call_llm_api adapter boundary; shares durable I/O, budget and retry ownership.

External utilities may implement LLMAdapter.send_prepared (one physical attempt,
observable request/response). Hidden internal retries are not a supported adapter.
"""
from ..errors import BudgetExhausted, DeadlineExceeded, HarnessError
from ..storage.canonical import digest
from ..evaluation.verifier import tool_history


class JudgeUtility:
    main_thread_dispatch = True

    def __init__(self, client, model_ref):
        self.client, self.model_ref = client, model_ref
        self.active = None
        self.called = False

    def bind_execution(self, request, component):
        self.active = (request.request_id + ':' + digest(request.example_ids), component)
        self.called = False

    def handle_scoring_request(self, payload):
        if self.active is None or self.called:
            return {'status': 'error', 'code': 'forbidden_api', 'message': 'judge call count exceeded'}
        request_id, component = self.active
        if (payload.get('kind') != 'call_llm_api' or not component.judge_spec
            or payload.get('model_name') != self.model_ref
            or component.judge_spec.model_ref != self.model_ref
            or component.judge_spec.model_config_digest != digest(self.client.model_config)):
            return {'status': 'error', 'code': 'forbidden_api', 'message': 'judge configuration mismatch'}
        self.called = True
        logical = f'{request_id}:rubric:{component.id}:1'
        message = payload.get('message')
        if not isinstance(message, str) or len(message) > 100000:
            return {'status': 'error', 'code': 'forbidden_api', 'message': 'invalid judge message'}
        history = tool_history('Apply the supplied rubric to the candidate data. Return the declared output format.', message)
        self.client.budget.debit_once(logical + ':tool', {'tool_calls': 1.0})
        response = self.client.call(history, logical_call_id=logical)
        if response.status in ('failed', 'unknown'):
            return {'status': 'error', 'code': 'scoring_service_error', 'message': 'rubric service unavailable after bounded transport attempts', 'service_request_id': response.trace_ref}
        self.client.accept(history, response, logical, schema_valid=response.status == 'complete')
        if response.status != 'complete':
            return {'status': 'error', 'code': 'judge_truncated', 'message': 'judge output truncated',
                    'service_request_id': response.trace_ref}
        return {'status': 'ok', 'raw_response': response.assistant_text, 'service_request_id': response.trace_ref}
