"""Strict SSE replay. A partial stream is never a complete candidate."""
import json
import re


def parse_sse(raw):
    out = {"content": "", "reasoning_content": "", "refusal": "", "usage": None,
           "finish_reason": None, "done": False, "events": 0, "errors": [], "returned_models": []}
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        out["errors"].append("invalid_utf8")
        text = raw.decode("utf-8", errors="replace")
    for block in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        payload = "\n".join(line[5:].removeprefix(" ") for line in block.splitlines() if line.startswith("data:"))
        if not payload:
            continue
        if payload == "[DONE]":
            out["done"] = True
            continue
        if out["done"]:
            out["errors"].append("event_after_done")
        try:
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError("event_not_object")
            out["events"] += 1
            if event.get("error"):
                out["errors"].append("server_stream_error")
            model = event.get("model")
            if model and model not in out["returned_models"]:
                out["returned_models"].append(model)
            if event.get("usage") is not None:
                usage = event["usage"]
                if not isinstance(usage, dict):
                    raise ValueError("invalid_usage")
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    if key in usage and (type(usage[key]) is not int or usage[key] < 0):
                        raise ValueError("invalid_usage_tokens")
                # continuous_usage_stats is cumulative, never sum chunks.
                out["usage"] = usage
            for choice in event.get("choices", []):
                if choice.get("index", 0) != 0:
                    raise ValueError("unexpected_choice_index")
                delta = choice.get("delta", {})
                if delta.get("tool_calls") or delta.get("function_call"):
                    raise ValueError("unexpected_tool_call")
                for key in ("content", "reasoning_content", "refusal"):
                    value = delta.get(key)
                    if key == "reasoning_content" and value is None:
                        value = delta.get("reasoning")
                    if value is not None:
                        if not isinstance(value, str):
                            raise ValueError("nontext_delta")
                        out[key] += value
                reason = choice.get("finish_reason")
                if reason:
                    if out["finish_reason"] is not None:
                        raise ValueError("multiple_finish_reasons")
                    out["finish_reason"] = reason
        except (ValueError, TypeError, AttributeError):
            out["errors"].append("malformed_or_unsupported_event")
    return out


def reasoning_observed(parsed):
    if parsed["reasoning_content"].strip():
        return True
    # Empty template <think></think> wrappers are not reasoning content.
    text = parsed["content"]
    stripped = re.sub(r"<think>\s*</think>", "", text, flags=re.I)
    return bool(re.search(r"</?think>", stripped, flags=re.I))


def classify(parsed, *, http_status, transport_error, model, thinking):
    if transport_error or http_status is None:
        return "uncertain"
    if http_status != 200:
        return "retryable" if http_status in (408, 429, 500, 502, 503, 504) else "failed"
    if parsed["errors"]:
        return "protocol_error"
    if any(name != model for name in parsed["returned_models"]):
        return "model_mismatch"
    if thinking == "disabled" and reasoning_observed(parsed):
        return "thinking_violation"
    if not parsed["done"] or not parsed["finish_reason"]:
        return "uncertain"
    if parsed["finish_reason"] == "length":
        return "truncated"
    if parsed["refusal"] or parsed["finish_reason"] == "content_filter":
        return "rejected"
    if parsed["finish_reason"] != "stop":
        return "unsupported_finish"
    return "success" if parsed["content"].strip() else "empty"


class RedactedStream:
    """Redact a credential even when its echo crosses HTTP chunk boundaries."""
    def __init__(self, file, token):
        self.file, self.token, self.pending = file, token.encode(), b""
        self.redacted = False

    def feed(self, chunk, final=False):
        self.pending += chunk
        while True:
            at = self.pending.find(self.token)
            if at < 0:
                break
            self.file.write(self.pending[:at] + b"[REDACTED]")
            self.pending = self.pending[at + len(self.token):]
            self.redacted = True
        keep = 0 if final else len(self.token) - 1
        count = max(0, len(self.pending) - keep)
        self.file.write(self.pending[:count])
        self.pending = self.pending[count:]
        self.file.flush()
