"""Append-only, prefix-stable model history for one episode.

    C_t   = P_run + P_episode + H_t
    H_t+1 = H_t + A_t + O_t + S_t+1

``P_run`` (instructions, tool schemas and their order, output format, reward
ABI, static catalogue) and ``P_episode`` (query, task contract, mode, initial
budget, library snapshot, initial resources) are frozen. ``H`` only ever grows
at the tail.

Before every request the history asserts that each already-visible message is
byte-identical in content, role and position to what the model saw last time.
The assertion is over the *normalized message sequence*; it deliberately does
not claim that a provider's HTTP bytes or chat template are prefix-stable,
because that is not something this layer can promise.

Budget/state updates are appended at the tail through the one declared
channel — a harness observation message — never injected into old messages and
never faked as a provider ``tool`` role that the configured API does not have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..errors import HarnessError
from ..schemas import Message
from ..storage.canonical import digest, text_digest


class PrefixViolation(HarnessError):
    """Raised when an already-visible message would change."""


def message_digest(message: Message) -> str:
    return digest({"role": message.role, "content": message.content})


@dataclass
class EpisodeHistory:
    run_prefix: tuple[Message, ...]
    episode_prefix: tuple[Message, ...]
    tail: list[Message] = field(default_factory=list)
    #: Digest of every message the model has already been shown, in order.
    _committed: list[str] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if not self._committed:
            self._committed = [message_digest(m) for m in self.prefix_messages]

    # -- views -----------------------------------------------------------
    @property
    def prefix_messages(self) -> tuple[Message, ...]:
        return tuple(self.run_prefix) + tuple(self.episode_prefix)

    def messages(self) -> list[Message]:
        return list(self.prefix_messages) + list(self.tail)

    @property
    def cursor(self) -> int:
        """Number of appended (non-prefix) messages confirmed so far."""
        return len(self.tail)

    def prefix_digest(self) -> str:
        return digest(
            [{"role": m.role, "content": m.content} for m in self.prefix_messages]
        )

    def history_digest(self) -> str:
        return digest([{"role": m.role, "content": m.content} for m in self.tail])

    def full_digest(self) -> str:
        return digest([{"role": m.role, "content": m.content} for m in self.messages()])

    # -- invariants --------------------------------------------------------
    def assert_prefix_stable(self) -> None:
        current = [message_digest(m) for m in self.messages()]
        if len(current) < len(self._committed):
            raise PrefixViolation(
                f"history shrank: {len(current)} messages now, "
                f"{len(self._committed)} were already shown to the model"
            )
        for i, (old, new) in enumerate(zip(self._committed, current)):
            if old != new:
                shown = self.messages()[i]
                raise PrefixViolation(
                    f"message {i} ({shown.role}/{shown.actor}) changed after it was "
                    "already visible to the model; history is append-only"
                )

    def mark_visible(self) -> None:
        """Record the current sequence as what the model has now seen."""
        self._committed = [message_digest(m) for m in self.messages()]

    # -- mutation ----------------------------------------------------------
    def append(self, message: Message) -> int:
        """Append one message. Returns the new cursor."""
        self.assert_prefix_stable()
        self.tail.append(message)
        return len(self.tail)

    def append_assistant(self, content: str, *, meta: dict | None = None) -> int:
        return self.append(
            Message(
                role="assistant",
                content=content,
                actor="assistant",
                trainable=True,
                meta=meta or {},
            )
        )

    def append_observation(self, content: str, *, meta: dict | None = None) -> int:
        """The single declared channel for tool results and state updates."""
        return self.append(
            Message(
                role="user",
                content=content,
                actor="harness_observation",
                trainable=False,
                meta=meta or {},
            )
        )

    # -- persistence -------------------------------------------------------
    def snapshot_refs(self, blobs) -> list[str]:
        return [blobs.put_json(m.model_dump(mode="json")) for m in self.messages()]

    @staticmethod
    def restore(
        run_prefix: tuple[Message, ...],
        episode_prefix: tuple[Message, ...],
        tail: list[Message],
    ) -> "EpisodeHistory":
        history = EpisodeHistory(run_prefix, episode_prefix, list(tail))
        history.mark_visible()
        return history


def estimate_tokens(text: str, counter: str = "chars_div4_conservative") -> int:
    """Token estimate used for context pre-checks.

    chars_div4_conservative counts UTF-8 bytes / 4 (rounded up) plus message overhead. Measured on
    English/JSON synthesis traffic this stays above the provider's prompt_tokens (about 4.3 bytes per
    token); it can undercount dense CJK text. Any other counter falls back to one token per UTF-8 byte,
    the upper bound; the harness never claims a tokenizer-accurate count it cannot produce.
    """
    n = len(text.encode("utf-8"))
    if counter == "chars_div4_conservative":
        return -(-n // 4) + 16
    return n + 16


def history_token_estimate(messages: list[Message], counter: str) -> int:
    return sum(estimate_tokens(m.content, counter) for m in messages)


def truncate_observation(text: str, max_chars: int, marker: str) -> tuple[str, bool]:
    """Bound an observation the first time it enters the context.

    Once bounded, the message is frozen; older observations are never
    re-compressed, because that would rewrite what the model already saw.
    """
    if len(text) <= max_chars:
        return text, False
    keep = max(0, max_chars - len(marker))
    return text[:keep] + marker, True


def content_ref(text: str) -> str:
    return text_digest(text)
