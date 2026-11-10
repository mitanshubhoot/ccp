"""The Anthropic client against a fake SDK: a malformed structured answer is asked about once
more instead of being terminal, a streamed answer forwards its deltas and is never replayed
after output began, and thinking blocks come back verbatim in the assistant turn."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, SecretStr

from ccp.config import Settings
from ccp.llm.anthropic_client import REASK_SUFFIX, AnthropicClient
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import SystemBlock
from ccp.llm.errors import TerminalModelError


class _Fields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str


class _Block(BaseModel):
    type: str
    text: str | None = None
    thinking: str | None = None
    signature: str | None = None
    id: str | None = None
    name: str | None = None
    input: dict[str, Any] | None = None


def _response(
    parsed: _Fields | None, *, stop: str = "end_turn", blocks: list[_Block] | None = None
) -> Any:
    return SimpleNamespace(
        parsed_output=parsed,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        stop_reason=stop,
        model="claude-haiku-4-5-20251001",
        content=blocks or [],
        to_dict=lambda: {"stop_reason": stop},
    )


class _Stream:
    def __init__(self, deltas: list[str], final: Any, fail_after: int | None = None) -> None:
        self.deltas, self.final, self.fail_after = deltas, final, fail_after

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *args: Any) -> bool:
        return False

    @property
    def text_stream(self) -> Any:
        for i, delta in enumerate(self.deltas):
            if self.fail_after is not None and i == self.fail_after:
                raise ConnectionError("socket closed mid-stream")
            yield delta

    def get_final_message(self) -> Any:
        return self.final


class _Messages:
    def __init__(self, parse_script: list[Any], streams: list[Any] | None = None) -> None:
        self.parse_script = parse_script
        self.streams = streams or []
        self.parse_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []

    def parse(self, **kwargs: Any) -> Any:
        self.parse_calls.append(kwargs)
        item = self.parse_script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def stream(self, **kwargs: Any) -> Any:
        return self.streams.pop(0)

    def create(self, **kwargs: Any) -> Any:
        self.create_calls.append(kwargs)
        return self.parse_script.pop(0)


def _client(messages: _Messages) -> AnthropicClient:
    settings = Settings(anthropic_api_key=SecretStr("k"))
    return AnthropicClient(
        settings, client=SimpleNamespace(messages=messages), sleep=lambda s: None
    )


def _tracker() -> BudgetTracker:
    return BudgetTracker(
        RunBudget(max_usd=1.0, max_tokens=100_000, max_seconds=60, max_tool_calls=10)
    )


def test_a_malformed_answer_is_asked_about_once_more_then_parsed() -> None:
    from pydantic import TypeAdapter

    bad: Exception
    try:
        TypeAdapter(_Fields).validate_json('{"answer": ')  # what the SDK raises inside parse
    except Exception as exc:  # noqa: BLE001
        bad = exc
    messages = _Messages([bad, _response(_Fields(answer="ok"))])
    out = _client(messages).extract(
        schema=_Fields,
        system=[SystemBlock("s")],
        user="u",
        model="claude-haiku-4-5-20251001",
        tracker=_tracker(),
    )
    assert out.parsed.answer == "ok"
    assert len(messages.parse_calls) == 2
    assert messages.parse_calls[1]["messages"][0]["content"] == "u" + REASK_SUFFIX
    assert out.usage.input_tokens == 10  # only the answer that parsed was charged


def test_two_malformed_answers_are_terminal() -> None:
    messages = _Messages([ValueError("bad json"), ValueError("bad json again")])
    with pytest.raises(TerminalModelError, match="re-ask"):
        _client(messages).extract(
            schema=_Fields,
            system=[SystemBlock("s")],
            user="u",
            model="claude-haiku-4-5-20251001",
            tracker=_tracker(),
        )


def test_streamed_deltas_are_forwarded_and_parsed() -> None:
    final = _response(_Fields(answer="ok"))
    messages = _Messages([], streams=[_Stream(['{"ans', 'wer": "ok"}'], final)])
    tokens: list[str] = []
    out = _client(messages).extract(
        schema=_Fields,
        system=[SystemBlock("s")],
        user="u",
        model="claude-haiku-4-5-20251001",
        tracker=_tracker(),
        on_token=tokens.append,
    )
    assert "".join(tokens) == '{"answer": "ok"}' and out.parsed.answer == "ok"


def test_a_stream_that_breaks_after_output_began_is_not_replayed() -> None:
    final = _response(_Fields(answer="ok"))
    messages = _Messages(
        [], streams=[_Stream(['{"ans', 'wer": "ok"}'], final, fail_after=1), _Stream(["x"], final)]
    )
    tokens: list[str] = []
    with pytest.raises(TerminalModelError, match="after output began"):
        _client(messages).extract(
            schema=_Fields,
            system=[SystemBlock("s")],
            user="u",
            model="claude-haiku-4-5-20251001",
            tracker=_tracker(),
            on_token=tokens.append,
        )
    assert tokens == ['{"ans']  # nothing was emitted twice
    assert len(messages.streams) == 1  # the second stream was never opened


def test_thinking_blocks_return_verbatim_in_the_assistant_turn() -> None:
    blocks = [
        _Block(type="thinking", thinking="let me look", signature="sig-1"),
        _Block(type="text", text="Searching."),
        _Block(type="tool_use", id="t1", name="search_evidence", input={"query": "x"}),
    ]
    messages = _Messages([_response(None, stop="tool_use", blocks=blocks)])
    turn = _client(messages).chat(
        system=[SystemBlock("s")],
        messages=[{"role": "user", "content": "go"}],
        tools=[{"name": "search_evidence"}],
        model="claude-haiku-4-5-20251001",
        tracker=_tracker(),
        reasoning=True,
    )
    assert [b["type"] for b in turn.assistant_content] == ["thinking", "text", "tool_use"]
    assert turn.assistant_content[0] == {
        "type": "thinking",
        "thinking": "let me look",
        "signature": "sig-1",
    }
    assert turn.tool_calls[0].name == "search_evidence" and turn.stop_reason == "tool_use"
