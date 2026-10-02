"""Parsing and validating the controller's action envelope.

One assistant message carries one batch:

    {"actions": [{"id": "a1", "tool": "read_resource", "arguments": {...}}]}

Parsing is deliberately strict about *structure* and forgiving about
*surrounding prose*: a model that wraps the envelope in a fenced block or adds
a sentence before it still gets its batch executed, but a malformed envelope,
an unknown tool, a duplicate action id or a truncated response never does.

A rejected envelope is a causal error owned by the agent: the harness returns a
structured observation naming the exact violation and the expected schema, and
does not silently repair or guess what the model meant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..errors import Code, ErrorCategory, RetryOwner
from ..schemas import ProposedAction, StructuredError

#: The envelope key. Fixed by the run prefix; never negotiated per episode.
ENVELOPE_KEY = "actions"

#: Tools that must be alone in their batch. Submitting is a terminal, ordering
#: sensitive commit; mixing it with speculative reads would make the commit
#: point ambiguous.
EXCLUSIVE_TOOLS = frozenset({"submit_reward"})

MAX_ACTIONS_PER_BATCH = 8


@dataclass
class ParseOutcome:
    actions: list[ProposedAction] = field(default_factory=list)
    error: StructuredError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _reject(code: str, message: str, suggestion: str) -> ParseOutcome:
    return ParseOutcome(
        error=StructuredError(
            category=ErrorCategory.PROTOCOL,
            code=code,
            phase="parse_actions",
            retry_owner=RetryOwner.AGENT,
            action_outcome="failed",
            message=message,
            suggested_recovery=suggestion,
        )
    )


_SCHEMA_HINT = (
    'Reply with exactly one JSON object of the form '
    '{"actions": [{"id": "<unique string>", "tool": "<tool name>", '
    '"arguments": {...}}]} and nothing else that looks like an envelope.'
)


def extract_envelope(text: str) -> tuple[dict[str, Any] | None, str | None]:
    """Find the single action envelope in an assistant message.

    Returns ``(envelope, error_message)``. Prose and fenced code blocks around
    the envelope are tolerated; two envelopes are not, because then the intended
    batch is genuinely ambiguous.
    """
    stripped = text.strip()
    if not stripped:
        return None, "assistant message was empty"

    candidates: list[dict[str, Any]] = []
    for start in _object_starts(stripped):
        obj = _try_object_at(stripped, start)
        if obj is not None and ENVELOPE_KEY in obj:
            candidates.append(obj)

    if not candidates:
        # An outer object that fails to decode would otherwise be reported via
        # whichever inner object does decode ("no 'actions' key"), hiding the
        # real mistake. Report the outermost decode failure instead.
        broken = _outer_decode_problem(stripped)
        if broken is not None:
            return None, broken
        # Distinguish "no JSON at all" from "JSON, but not an envelope" so the
        # observation can tell the model which mistake it made.
        for start in _object_starts(stripped):
            if _try_object_at(stripped, start) is not None:
                return None, (
                    f"found a JSON object but it has no {ENVELOPE_KEY!r} key"
                )
        return None, "no parseable JSON object found in the message"

    if len(candidates) > 1:
        return None, (
            f"found {len(candidates)} action envelopes in one message; "
            "exactly one is allowed"
        )
    return candidates[0], None


def _object_starts(text: str) -> list[int]:
    return [i for i, ch in enumerate(text) if ch == "{"]


_EXCERPT_CHARS = 60
_CLOSERS = {"}": "{", "]": "["}


def _outer_decode_problem(text: str) -> str | None:
    """Describe why the intended envelope does not decode, or None if it does.

    The intended envelope is the first object that opens with the envelope key;
    without one, the first object if it starts like a JSON object (``{"``).
    Braces in surrounding prose are therefore not reported as JSON errors.
    """
    starts = _object_starts(text)
    if not starts:
        return None
    marked = [s for s in starts if text[s + 1:].lstrip().startswith(f'"{ENVELOPE_KEY}"')]
    if marked:
        start = marked[0]
    elif text[starts[0] + 1:].lstrip().startswith('"'):
        start = starts[0]
    else:
        return None
    try:
        json.JSONDecoder().raw_decode(text, start)
        return None
    except json.JSONDecodeError as exc:
        pos = exc.pos
        where = f"{exc.msg} at line {exc.lineno} column {exc.colno} (char {pos})"
    lo, hi = max(0, pos - _EXCERPT_CHARS), pos + _EXCERPT_CHARS
    excerpt = json.dumps(text[lo:pos]) + " <HERE> " + json.dumps(text[pos:hi])
    message = (f"the JSON object starting at char {start} is not valid JSON: {where}; "
               f"text around the error: {excerpt}")
    brackets = _bracket_problem(text, start)
    if brackets:
        message += f"; {brackets}"
    return message + ". Fix the JSON syntax and resend the complete envelope"


def _bracket_problem(text: str, start: int) -> str | None:
    """First bracket mismatch outside JSON strings, scanning from ``start``."""
    stack: list[tuple[str, int]] = []
    in_string = escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append((ch, i))
        elif ch in _CLOSERS:
            if not stack:
                return f"unmatched {ch!r} at char {i}"
            opener, at = stack.pop()
            if opener != _CLOSERS[ch]:
                return (f"bracket mismatch: {ch!r} at char {i} closes the {opener!r} "
                        f"opened at char {at}; a closing bracket is missing or extra")
            if not stack:
                return None
    if in_string:
        return "a string literal is not terminated"
    if stack:
        return (f"{len(stack)} bracket(s) never closed: "
                + ", ".join(f"{o!r} at char {at}" for o, at in stack[-5:]))
    return None


def _try_object_at(text: str, start: int) -> dict[str, Any] | None:
    """Decode the JSON object beginning at ``start``, if there is one."""
    decoder = json.JSONDecoder()
    try:
        obj, _ = decoder.raw_decode(text, start)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_actions(
    text: str,
    *,
    known_tools: frozenset[str] | set[str],
    finish_reason: str | None = None,
    max_actions: int = MAX_ACTIONS_PER_BATCH,
) -> ParseOutcome:
    """Turn assistant text into a validated batch, or a structured rejection."""

    # Truncation is checked first. A cut-off message may still contain a
    # syntactically complete prefix envelope, and executing that would run a
    # batch the model never finished composing.
    if finish_reason == "length":
        return _reject(
            Code.TRUNCATED_OUTPUT,
            "response was truncated by the output token limit, so no action "
            "batch was executed",
            "Reply again with a shorter batch that fits the output limit.",
        )

    envelope, problem = extract_envelope(text)
    if envelope is None:
        return _reject(
            Code.INVALID_ACTION_SCHEMA, f"{problem}. {_SCHEMA_HINT}", _SCHEMA_HINT
        )

    raw = envelope[ENVELOPE_KEY]
    if not isinstance(raw, list):
        return _reject(
            Code.INVALID_ACTION_SCHEMA,
            f"{ENVELOPE_KEY!r} must be a list, got {type(raw).__name__}",
            _SCHEMA_HINT,
        )
    if not raw:
        return _reject(
            Code.EMPTY_ACTION_BATCH,
            "the action batch was empty; every turn must either request at "
            "least one action or submit",
            "Issue at least one action, or call submit_reward to finish.",
        )
    if len(raw) > max_actions:
        return _reject(
            Code.INVALID_ACTION_SCHEMA,
            f"batch has {len(raw)} actions; at most {max_actions} are allowed",
            f"Split the work across turns, at most {max_actions} actions each.",
        )

    actions: list[ProposedAction] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            return _reject(
                Code.INVALID_ACTION_SCHEMA,
                f"action {index} is {type(item).__name__}, expected an object",
                _SCHEMA_HINT,
            )
        extra = set(item) - {"id", "tool", "arguments"}
        if extra:
            return _reject(
                Code.INVALID_ACTION_SCHEMA,
                f"action {index} has unexpected keys {sorted(extra)}",
                _SCHEMA_HINT,
            )
        action_id = item.get("id")
        tool = item.get("tool")
        arguments = item.get("arguments", {})
        if not isinstance(action_id, str) or not action_id.strip():
            return _reject(
                Code.INVALID_ACTION_SCHEMA,
                f"action {index} needs a non-empty string 'id'",
                _SCHEMA_HINT,
            )
        if action_id in seen_ids:
            return _reject(
                Code.DUPLICATE_ACTION_ID,
                f"action id {action_id!r} appears twice in one batch; ids must "
                "be unique so results can be matched to requests",
                "Give each action in the batch a distinct id.",
            )
        if not isinstance(tool, str) or tool not in known_tools:
            return _reject(
                Code.UNKNOWN_TOOL,
                f"action {action_id!r} names tool {tool!r}, which does not "
                f"exist; available tools: {sorted(known_tools)}",
                "Use one of the tools declared in the system prefix.",
            )
        if not isinstance(arguments, dict):
            return _reject(
                Code.INVALID_ACTION_SCHEMA,
                f"action {action_id!r} has 'arguments' of type "
                f"{type(arguments).__name__}, expected an object",
                _SCHEMA_HINT,
            )
        seen_ids.add(action_id)
        actions.append(ProposedAction(id=action_id, tool=tool, arguments=arguments))

    exclusive = [a for a in actions if a.tool in EXCLUSIVE_TOOLS]
    if exclusive and len(actions) > 1:
        return _reject(
            Code.BATCH_RULE_VIOLATION,
            f"{exclusive[0].tool!r} must be the only action in its batch; this "
            f"batch also contains {[a.tool for a in actions if a.tool not in EXCLUSIVE_TOOLS]}",
            "Send the submit_reward call on its own turn.",
        )

    return ParseOutcome(actions=actions)


def render_rejection(error: StructuredError) -> str:
    """The observation text shown to the model after a rejected envelope."""
    return json.dumps(
        {
            "type": "protocol_error",
            "code": error.code,
            "message": error.message,
            "expected_format": _SCHEMA_HINT,
            "retry_owner": error.retry_owner,
        },
        ensure_ascii=False,
        indent=2,
    )
