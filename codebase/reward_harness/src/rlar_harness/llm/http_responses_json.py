"""Text-only Responses adapter sharing transport, retry ownership and raw I/O.

AIHub's provider selector is appended only to the runtime Authorization header.
No server-side conversation, tools, or automatic provider fallback is used.
"""
from urllib.parse import urlencode

from .adapter import ProviderError, ProviderResponse
from .http_chat_json import HttpChatJsonAdapter, _as_int


class HttpResponsesJsonAdapter(HttpChatJsonAdapter):
    name = 'http_responses_json_v1'

    def build_headers(self):
        headers = super().build_headers()
        if self.config.auth_provider:
            if 'authorization' not in headers:
                raise ValueError('AIHub provider routing requires a runtime API key')
            headers['authorization'] += '?' + urlencode({'provider': self.config.auth_provider})
        return headers

    def build_body(self, request):
        body = {'model': self.config.model,
                'input': [{'role': m.role, 'content': m.content} for m in request.messages],
                'max_output_tokens': request.max_output_tokens,
                'stream': False, 'store': self.config.responses_store}
        if self.config.send_temperature:
            body['temperature'] = request.temperature
        if self.config.top_p is not None:
            body['top_p'] = self.config.top_p
        if self.config.prompt_cache_key is not None:
            body['prompt_cache_key'] = self.config.prompt_cache_key
        if self.config.reasoning_effort is not None:
            body['reasoning'] = {'effort': self.config.reasoning_effort}
        if self.config.response_format:
            body['text'] = {'format': {'type': self.config.response_format}}
        return body

    def parse_response(self, response):
        data = self.response_object(response)
        status = data.get('status')
        if data.get('error') or status not in ('completed', 'incomplete'):
            raise ProviderError('Responses request did not complete: ' + str(status), dispatched=True)
        incomplete = data.get('incomplete_details')
        if status == 'incomplete' and (not isinstance(incomplete, dict) or incomplete.get('reason') != 'max_output_tokens'):
            raise ProviderError('Responses output incomplete for a non-token-limit reason', dispatched=True)
        output = data.get('output')
        if not isinstance(output, list):
            raise ProviderError('Responses output must be an array', dispatched=True)
        chunks = []
        for item in output:
            if not isinstance(item, dict):
                raise ProviderError('invalid Responses output item', dispatched=True)
            if item.get('type') == 'reasoning':
                continue  # Raw response is retained; reasoning is not assistant output.
            if item.get('type') != 'message' or item.get('role') != 'assistant':
                raise ProviderError('unexpected Responses output/tool item', dispatched=True)
            if item.get('status') not in ('completed', 'incomplete'):
                raise ProviderError('Responses message is not complete', dispatched=True)
            if status == 'completed' and item['status'] != 'completed':
                raise ProviderError('completed response contains an incomplete message', dispatched=True)
            content = item.get('content')
            if not isinstance(content, list):
                raise ProviderError('Responses message content must be an array', dispatched=True)
            for part in content:
                if not isinstance(part, dict) or part.get('type') != 'output_text' or not isinstance(part.get('text'), str):
                    raise ProviderError('refused or unsupported Responses content', dispatched=True)
                chunks.append(part['text'])
        if not chunks and status == 'completed':
            raise ProviderError('Responses completed without assistant text', dispatched=True)
        usage = data.get('usage')
        usage = usage if isinstance(usage, dict) else {}
        prompt = _as_int(usage.get('input_tokens'))
        completion = _as_int(usage.get('output_tokens'))
        details = usage.get('input_tokens_details')
        details = details if isinstance(details, dict) else {}
        return ProviderResponse(text=''.join(chunks), finish_reason='stop' if status == 'completed' else 'length',
            provider_request_id=data.get('id') if isinstance(data.get('id'), str) else None,
            prompt_tokens=prompt, completion_tokens=completion,
            usage_known=prompt is not None and completion is not None,
            cached_tokens=_as_int(details.get('cached_tokens')))
