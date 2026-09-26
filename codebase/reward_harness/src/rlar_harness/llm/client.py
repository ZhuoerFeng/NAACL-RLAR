"""Public unified LLM boundary. Every controller call requires durable storage.

The earlier in-memory prototype has been superseded by the journal-backed
implementation; importing LLMClient cannot silently opt out of request capture.
"""
from ..schemas import LLMRequest
from ..storage.canonical import digest
from .durable import DurableLLMClient

LLMClient = DurableLLMClient

def request_digest(request: LLMRequest) -> str:
    """Identity of the physical request payload.

    Covers exactly what goes on the wire as content plus the sampling
    parameters that change the distribution. Deliberately excludes the logical
    call id, the budget snapshot, the deadline and every header — so a transport
    retry of the same turn produces the same digest, and a credential never
    enters a hash that gets written to a trace.
    """
    return digest(
        {
            "messages": [
                {"role": m.role, "content": m.content} for m in request.messages
            ],
            "model_config_ref": request.model_config_ref,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
        }
    )


def llm_call(client, history, *, logical_call_id):
    return client.call(history, logical_call_id=logical_call_id)
