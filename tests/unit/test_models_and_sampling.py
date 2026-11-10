"""NT8 (spec.md A-103): pinned model ids, no sampling parameter on any request, thinking and
effort per call type the way each model accepts them, the API version sent by the client
itself, and the 5.5 prices, Haiku 5.5's long-prompt rate card included."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr

from ccp.config import Settings
from ccp.llm import models
from ccp.llm.anthropic_client import REASONING_MIN_MAX_TOKENS, AnthropicClient, BatchItem
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import SystemBlock
from ccp.llm.errors import TerminalModelError, UnpricedModel
from ccp.llm.models import MODELS, UnpinnedModel, family, sampling_params, spec_for
from ccp.observability import cost

SAMPLING = ("temperature", "top_p", "top_k", "extra_body")


class _Fields(BaseModel):
    answer: str


def _tracker() -> BudgetTracker:
    return BudgetTracker(RunBudget(max_tokens=10**7, max_usd=100.0, max_seconds=600))


def _response(model: str) -> Any:
    return SimpleNamespace(
        parsed_output=_Fields(answer="x"),
        stop_reason="end_turn",
        model=model,
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=2,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        content=[SimpleNamespace(type="text", text='{"answer": "x"}')],
        to_dict=lambda: {"ok": True},
    )


def _client() -> tuple[AnthropicClient, list[dict[str, Any]]]:
    sent: list[dict[str, Any]] = []

    def call(**kwargs: Any) -> Any:
        sent.append(kwargs)
        return _response(kwargs["model"])

    fake = SimpleNamespace(messages=SimpleNamespace(parse=call, create=call))
    settings = Settings(anthropic_api_key=SecretStr("k"))
    return AnthropicClient(settings, client=fake, sleep=lambda s: None), sent


@pytest.mark.parametrize("model", sorted(MODELS))
def test_no_request_on_any_model_carries_a_sampling_parameter(model: str) -> None:
    """API version 2026-10-21 rejects temperature, top_p and top_k on every model, even at
    their defaults, and an extra_body entry still puts them in the JSON."""
    client, sent = _client()
    for reasoning in (False, True):
        client.extract(
            schema=_Fields,
            system=[SystemBlock("s")],
            user="u",
            model=model,
            tracker=_tracker(),
            reasoning=reasoning,
        )
        client.chat(
            system=[SystemBlock("s")],
            messages=[{"role": "user", "content": "u"}],
            tools=[],
            model=model,
            tracker=_tracker(),
            reasoning=reasoning,
        )
    client.complete(system=[SystemBlock("s")], user="u", model=model, tracker=_tracker())
    batch = BatchItem("b", system=[], user="u", model=model, schema=_Fields).params()
    for request in [*sent, batch]:
        assert not set(SAMPLING) & set(request), request


def test_thinking_off_is_spelled_the_way_each_model_accepts_it() -> None:
    off = {m: sampling_params(m, reasoning=False) for m in MODELS}
    medium = {"effort": "medium"}
    assert off["claude-haiku-5-5"] == {"thinking": {"type": "disabled"}, "output_config": medium}
    assert off["claude-sonnet-5"] == {"thinking": {"type": "disabled"}, "output_config": medium}
    assert off["claude-opus-5"] == {"thinking": {"type": "disabled"}, "output_config": medium}
    # Sonnet 5.5 rejects disabled: between_tools takes no other thinking field
    assert off["claude-sonnet-5-5"] == {
        "thinking": {"type": "between_tools"},
        "output_config": medium,
    }
    # Opus 5.5 and Fable always think: no thinking field, the least effort
    for model in ("claude-opus-5-5", "claude-fable-5-1"):
        assert off[model] == {"output_config": {"effort": "low"}}
    assert off["claude-haiku-4-5-20251001"] == {}  # no thinking unless asked, no effort


def test_reasoning_calls_think_adaptively_at_medium_effort() -> None:
    for model, spec in MODELS.items():
        params = sampling_params(model, reasoning=True)
        if spec.adaptive:
            assert params == {
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "medium"},
            }
        else:
            assert params == {}


def test_a_call_that_can_think_gets_room_and_one_that_cannot_keeps_its_cap() -> None:
    client, sent = _client()
    for model in ("claude-haiku-5-5", "claude-opus-5-5", "claude-sonnet-5-5"):
        client.complete(
            system=[SystemBlock("s")], user="u", model=model, tracker=_tracker(), max_tokens=512
        )
    caps = {r["model"]: r["max_tokens"] for r in sent}
    assert caps["claude-haiku-5-5"] == 512  # thinking disabled: the cap is the answer's
    assert caps["claude-opus-5-5"] == REASONING_MIN_MAX_TOKENS  # thinks whatever it is told
    assert caps["claude-sonnet-5-5"] == REASONING_MIN_MAX_TOKENS  # between_tools may think
    assert models.thinks("claude-haiku-5-5", {}) is True  # thinks when nothing is said


def test_aliases_floating_and_unknown_ids_are_refused_before_any_call() -> None:
    client, sent = _client()
    with pytest.raises(UnpinnedModel, match="claude-haiku-4-5-20251001"):
        client.extract(
            schema=_Fields, system=[], user="u", model="claude-haiku-4-5", tracker=_tracker()
        )
    with pytest.raises(UnpinnedModel, match="floats"):
        client.complete(system=[], user="u", model="claude-sonnet-5-5-latest", tracker=_tracker())
    with pytest.raises(UnpricedModel):
        client.complete(system=[], user="u", model="claude-opus-9", tracker=_tracker())
    with pytest.raises(UnpinnedModel):
        client.submit_batch([BatchItem("b", system=[], user="u", model="claude-haiku-4-5")])
    assert sent == [] and issubclass(UnpinnedModel, TerminalModelError)


def test_the_default_models_are_pinned_priced_and_tier_2_is_haiku_5_5() -> None:
    defaults = {
        name: Settings.model_fields[name].default
        for name in ("ccp_tier2_model", "ccp_tier3_model", "ccp_judge_model")
    }
    assert defaults["ccp_tier2_model"] == "claude-haiku-5-5"
    for model in defaults.values():
        assert spec_for(model).id == model and cost.is_priced(model)
    for model in MODELS:
        assert cost.is_priced(model), f"{model} has no price"


def test_the_client_sends_the_pinned_api_version_itself() -> None:
    client = AnthropicClient(Settings(anthropic_api_key=SecretStr("k")))
    headers = client._client.default_headers  # noqa: SLF001 - what every request carries
    assert headers["anthropic-version"] == models.ANTHROPIC_API_VERSION == "2026-10-21"


def test_haiku_5_5_bills_long_prompts_on_its_second_rate_card() -> None:
    short = cost.estimate_usd("claude-haiku-5-5", input_tokens=100_000, output_tokens=1_000)
    assert short == pytest.approx(100_000 * 0.10 / 1e6 + 1_000 * 0.50 / 1e6)
    long = cost.estimate_usd("claude-haiku-5-5", input_tokens=100_001, output_tokens=1_000)
    assert long == pytest.approx(100_001 * 0.50 / 1e6 + 1_000 * 2.50 / 1e6)
    # cached input counts toward the prompt length that picks the card
    cached = cost.estimate_usd("claude-haiku-5-5", input_tokens=1_000, cache_read_tokens=150_000)
    assert cached == pytest.approx(1_000 * 0.50 / 1e6 + 150_000 * 0.05 / 1e6)


def test_opus_5_5_and_sonnet_5_5_are_priced_as_themselves() -> None:
    assert cost.estimate_usd("claude-opus-5-5", input_tokens=10**6) == pytest.approx(4.0)
    assert cost.estimate_usd("claude-opus-5", input_tokens=10**6) == pytest.approx(5.0)
    assert cost.price_for("claude-opus-5-5").cache_read == pytest.approx(0.20)
    assert cost.estimate_usd("claude-sonnet-5-5", output_tokens=10**6) == pytest.approx(10.0)


def test_batch_items_turn_thinking_off_and_keep_the_schema() -> None:
    params = BatchItem("b", system=[], user="u", model="claude-haiku-5-5", schema=_Fields).params()
    assert params["thinking"] == {"type": "disabled"}
    assert params["output_config"]["effort"] == "medium"
    assert params["output_config"]["format"]["type"] == "json_schema"


def test_model_families() -> None:
    assert family("claude-opus-5") == "anthropic" and family("stub:claude-haiku-5-5") == "anthropic"
    assert family("gpt-5") == "openai" and family("o3-mini") == "openai"
    assert family("gemini-3-pro") == "google" and family("mystery") == "unknown"
