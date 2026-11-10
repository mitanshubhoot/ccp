"""The keyless embedding client, the price table, the retrieval config, and the Anthropic
client against a fake SDK object (no network)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from ccp.config import Settings
from ccp.llm import anthropic_client
from ccp.llm.anthropic_client import AnthropicClient, BatchItem, system_param
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import SystemBlock
from ccp.llm.errors import RetryableModelError, TerminalModelError
from ccp.observability import cost
from ccp.retrieval import config as retrieval_config
from ccp.retrieval.config import RetrievalConfig, Thresholds, quality_from_thresholds
from ccp.retrieval.embeddings import HashingEmbeddingClient, parse_vector, vector_literal


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def test_hashing_embeddings_are_deterministic_unit_vectors_that_track_overlap() -> None:
    client = HashingEmbeddingClient()
    v1 = client.embed(["acknowledge every report within 5 business days"], "document").vectors[0]
    v2 = client.embed(["acknowledge every report within 5 business days"], "query").vectors[0]
    other = client.embed(["backups are retained for 35 days"], "document").vectors[0]
    assert len(v1) == 1024 and v1 == v2
    assert _cosine(v1, v1) == pytest.approx(1.0, abs=1e-6)
    assert _cosine(v1, other) < 0.5
    near = client.embed(["we acknowledge each report within 5 business days"], "query").vectors[0]
    assert _cosine(v1, near) > _cosine(v1, other)
    assert client.model_id == "local-hashing-v1"


def test_vector_literal_round_trips() -> None:
    vec = [0.5, -0.25, 1.0]
    assert parse_vector(vector_literal(vec)) == pytest.approx(vec)


def test_cost_table_estimates_from_list_prices() -> None:
    usd = cost.estimate_usd(
        "claude-haiku-4-5",
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_read_tokens=1_000_000,
    )
    assert usd == pytest.approx(1.0 + 0.5 + 0.10)
    assert cost.estimate_usd("claude-haiku-4-5-20251001", input_tokens=1_000_000) == pytest.approx(
        1.0
    )
    assert cost.estimate_usd("voyage-3-large", input_tokens=1_000_000) == pytest.approx(0.18)
    assert cost.estimate_usd("local-hashing-v1", input_tokens=10**9) == 0.0
    assert cost.estimate_usd(
        "claude-sonnet-5", input_tokens=1_000_000, batch=True
    ) == pytest.approx(1.0)
    assert cost.price_for("unknown-model").input == 0.0


def test_quality_from_thresholds_interpolates_the_noise_floor() -> None:
    t = Thresholds(
        measured_at="2026-09-11",
        queries=10,
        percentiles={50: 0.2, 90: 0.4, 99: 0.6},
        score_kind="fused",
    )
    assert quality_from_thresholds(0.1, t) == pytest.approx(0.25)
    assert quality_from_thresholds(0.3, t) == pytest.approx(0.7)
    assert quality_from_thresholds(0.9, t) == 1.0
    assert quality_from_thresholds(0.5, None) is None
    cfg = RetrievalConfig(thresholds={RetrievalConfig.key("m", None): t})
    assert cfg.thresholds_for("m", None) is t and cfg.thresholds_for("m", "r") is None


def test_retrieval_config_round_trips_through_yaml(tmp_path: Any) -> None:
    cfg = RetrievalConfig(
        ef_search=64,
        thresholds={
            "m|none": Thresholds(
                measured_at="2026-09-11", queries=3, percentiles={50: 0.1}, score_kind="fused"
            )
        },
    )
    path = tmp_path / "retrieval.yaml"
    retrieval_config.save(cfg, path)
    assert retrieval_config.load(path) == cfg
    assert retrieval_config.load(tmp_path / "missing.yaml") == RetrievalConfig()


# -- the Anthropic client against a fake SDK -------------------------------------------------


class _Fields(BaseModel):
    answer: str


def _response(
    parsed: Any, *, cache_read: int = 0, stop_reason: str = "end_turn", text: str = ""
) -> Any:
    return SimpleNamespace(
        parsed_output=parsed,
        stop_reason=stop_reason,
        model="claude-haiku-4-5-20251001",
        usage=SimpleNamespace(
            input_tokens=1000,
            output_tokens=50,
            cache_read_input_tokens=cache_read,
            cache_creation_input_tokens=0,
        ),
        content=[
            SimpleNamespace(type="text", text=text or (parsed.model_dump_json() if parsed else ""))
        ],
        to_dict=lambda: {"id": "msg_1", "stop_reason": stop_reason},
    )


class _FakeMessages:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []

    def _next(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def parse(self, **kwargs: Any) -> Any:
        return self._next(**kwargs)

    def create(self, **kwargs: Any) -> Any:
        return self._next(**kwargs)


def _client(script: list[Any]) -> tuple[AnthropicClient, _FakeMessages]:
    messages = _FakeMessages(script)
    fake = SimpleNamespace(messages=messages)
    return AnthropicClient(
        Settings(anthropic_api_key="k"), client=fake, sleep=lambda s: None
    ), messages


def _tracker() -> BudgetTracker:
    return BudgetTracker(RunBudget(max_tokens=100_000, max_usd=1.0, max_seconds=60))


def test_system_blocks_put_the_breakpoint_on_the_flagged_block_only() -> None:
    blocks = system_param([SystemBlock("stable"), SystemBlock("schema", cache=True)])
    assert blocks == [
        {"type": "text", "text": "stable"},
        {"type": "text", "text": "schema", "cache_control": {"type": "ephemeral"}},
    ]


def test_extract_parses_charges_and_records_the_request_and_response() -> None:
    client, messages = _client([_response(_Fields(answer="x"), cache_read=800)])
    tracker = _tracker()
    out = client.extract(
        schema=_Fields,
        system=[SystemBlock("s", cache=True)],
        user="u",
        model="claude-haiku-4-5",
        tracker=tracker,
    )
    assert out.parsed == _Fields(answer="x") and out.model == "claude-haiku-4-5-20251001"
    assert out.usage.cache_read_input_tokens == 800 and out.usage.usd == pytest.approx(
        cost.estimate_usd(
            "claude-haiku-4-5", input_tokens=1000, output_tokens=50, cache_read_tokens=800
        )
    )
    assert tracker.tokens == 1850 and out.request["output_format"] == "_Fields"
    assert out.response == {"id": "msg_1", "stop_reason": "end_turn"}
    assert (
        messages.calls[0]["output_format"] is _Fields
        and messages.calls[0]["system"][0]["cache_control"]
    )


def test_rate_limit_retries_then_succeeds_and_bad_request_is_terminal() -> None:
    import httpx2

    def status_error(cls: type[Exception], code: int) -> Exception:
        response = httpx2.Response(code, request=httpx2.Request("POST", "https://api"))
        return cls("err", response=response, body=None)  # type: ignore[call-arg]

    import anthropic

    client, messages = _client(
        [status_error(anthropic.RateLimitError, 429), _response(_Fields(answer="ok"))]
    )
    out = client.extract(
        schema=_Fields,
        system=[SystemBlock("s")],
        user="u",
        model="claude-haiku-4-5",
        tracker=_tracker(),
    )
    assert out.parsed.answer == "ok" and len(messages.calls) == 2

    client, _ = _client([status_error(anthropic.BadRequestError, 400)])
    with pytest.raises(TerminalModelError):
        client.extract(
            schema=_Fields,
            system=[SystemBlock("s")],
            user="u",
            model="claude-haiku-4-5",
            tracker=_tracker(),
        )

    client, messages = _client(
        [status_error(anthropic.InternalServerError, 500)] * anthropic_client.RETRY_ATTEMPTS
    )
    with pytest.raises(RetryableModelError):
        client.extract(
            schema=_Fields,
            system=[SystemBlock("s")],
            user="u",
            model="claude-haiku-4-5",
            tracker=_tracker(),
        )
    assert len(messages.calls) == anthropic_client.RETRY_ATTEMPTS


def test_schema_miss_is_re_asked_once_then_terminal_and_refusal_is_terminal() -> None:
    client, messages = _client([_response(None), _response(_Fields(answer="second"))])
    out = client.extract(
        schema=_Fields,
        system=[SystemBlock("s")],
        user="u",
        model="claude-haiku-4-5",
        tracker=_tracker(),
    )
    assert out.parsed.answer == "second" and len(messages.calls) == 2
    assert messages.calls[1]["messages"][0]["content"].endswith("every required field present.")

    client, _ = _client([_response(None), _response(None)])
    with pytest.raises(TerminalModelError, match="re-ask"):
        client.extract(
            schema=_Fields,
            system=[SystemBlock("s")],
            user="u",
            model="claude-haiku-4-5",
            tracker=_tracker(),
        )

    client, _ = _client([_response(_Fields(answer="x"), stop_reason="refusal")])
    with pytest.raises(TerminalModelError, match="refused"):
        client.extract(
            schema=_Fields,
            system=[SystemBlock("s")],
            user="u",
            model="claude-haiku-4-5",
            tracker=_tracker(),
        )


def test_complete_returns_the_text_blocks() -> None:
    client, _ = _client([_response(None, text="the context")])
    out = client.complete(
        system=[SystemBlock("s")], user="u", model="claude-haiku-4-5", tracker=_tracker()
    )
    assert out.text == "the context" and out.usage.input_tokens == 1000


def test_batch_items_carry_the_json_schema_output_config() -> None:
    item = BatchItem(
        "id-1",
        system=[SystemBlock("s", cache=True)],
        user="u",
        model="claude-haiku-4-5",
        schema=_Fields,
    )
    params = item.params()
    assert params["output_config"] == {
        "format": {"type": "json_schema", "schema": _Fields.model_json_schema()}
    }
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert (
        "output_config"
        not in BatchItem("id-2", system=[], user="u", model="claude-haiku-4-5").params()
    )
