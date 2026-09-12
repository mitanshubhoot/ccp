"""Token streaming for the Copilot (spec.md Layer 17, Layer 12): the answer is a structured
call whose JSON streams token by token; this turns the deltas into the prose of the ``text``
field as it arrives, so the terminal shows the answer while the rest of the object (citations,
requested actions) is still being generated. First-token latency is the time of the first
delta, whatever field it belongs to."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable

_KEY = re.compile(r'"text"\s*:\s*"')
# an incomplete escape, or a complete high surrogate that must wait for its low half
_PARTIAL_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{0,3}|u[dD][89abAB][0-9a-fA-F]{2})?$")


def _unescaped_quote(raw: str) -> int | None:
    i = 0
    while i < len(raw):
        c = raw[i]
        if c == "\\":
            i += 2
            continue
        if c == '"':
            return i
        i += 1
    return None


def _trim_partial_escape(raw: str) -> str:
    match = _PARTIAL_ESCAPE.search(raw)
    return raw[: match.start()] if match else raw


class TextFieldStreamer:
    def __init__(
        self,
        on_token: Callable[[str], None],
        *,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._on_token = on_token
        self._clock = clock
        self.buffer = ""
        self.first_token_at: float | None = None
        self.done = False
        self._start: int | None = None
        self._decoded = ""

    @property
    def text(self) -> str:
        """The prose emitted so far."""
        return self._decoded

    def feed(self, delta: str) -> None:
        if self.first_token_at is None:
            self.first_token_at = self._clock()
        self.buffer += delta
        if self.done:
            return
        if self._start is None:
            match = _KEY.search(self.buffer)
            if match is None:
                return
            self._start = match.end()
        raw = self.buffer[self._start :]
        end = _unescaped_quote(raw)
        if end is not None:
            raw = raw[:end]
            self.done = True
        else:
            raw = _trim_partial_escape(raw)
        try:
            decoded = json.loads(f'"{raw}"')
        except ValueError:
            return
        if len(decoded) > len(self._decoded):
            self._on_token(decoded[len(self._decoded) :])
            self._decoded = decoded
