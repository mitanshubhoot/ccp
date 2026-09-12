"""The text-field streamer turns structured-output JSON deltas into prose as it arrives."""

from __future__ import annotations

import json

from ccp.copilot.streaming import TextFieldStreamer


def _stream(payload: dict, size: int) -> tuple[str, list[str], TextFieldStreamer]:  # type: ignore[type-arg]
    raw = json.dumps(payload)
    tokens: list[str] = []
    clock = iter(range(1, 10_000))
    streamer = TextFieldStreamer(tokens.append, clock=lambda: float(next(clock)))
    for i in range(0, len(raw), size):
        streamer.feed(raw[i : i + size])
    return raw, tokens, streamer


def test_prose_is_emitted_incrementally_and_exactly_once() -> None:
    payload = {"text": 'Jane said "hi"\nthen left \\ ok é', "citations": [], "n": 1}
    for size in (1, 3, 7, 50, 500):
        _, tokens, streamer = _stream(payload, size)
        assert "".join(tokens) == payload["text"], size
        assert streamer.text == payload["text"] and streamer.done
        assert streamer.first_token_at == 1.0


def test_nothing_is_emitted_before_the_text_field_and_other_fields_are_ignored() -> None:
    payload = {"intent": "explain", "text": "answer", "citations": [{"quote": "text: nope"}]}
    _, tokens, streamer = _stream(payload, 4)
    assert "".join(tokens) == "answer" and streamer.done


def test_partial_unicode_escape_waits_for_the_rest() -> None:
    tokens: list[str] = []
    streamer = TextFieldStreamer(tokens.append)
    streamer.feed('{"text": "caf\\u00')
    assert tokens == ["caf"]
    streamer.feed('e9!", "x": 1}')
    assert "".join(tokens) == "café!" and streamer.done
