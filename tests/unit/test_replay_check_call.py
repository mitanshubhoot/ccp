"""Replay's per-call checks against synthetic requests (no database when nothing is fenced):
an edited prompt, an unknown schema, a foreign nonce, and a clean chat call."""

from __future__ import annotations

from typing import Any

from ccp.evaluators.extraction import schema_block, schema_model
from ccp.llm.prompts import load_prompt
from ccp.replay.rebuild import check_call


def _request(
    system_text: str, user: str, schema: str | None = "DisclosurePolicyFields"
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": "m",
        "system": [{"text": system_text, "cache": True}, {"text": "schema placeholder"}],
        "user": user,
    }
    if schema:
        request["output_format"] = schema
    return request


def test_an_edited_prompt_and_an_unknown_schema_are_reported() -> None:
    request = _request("EDITED PROMPT", "no fences here", schema="NoSuchSchema")
    call = check_call(
        None,  # type: ignore[arg-type]
        name="extract",
        request=request,
        response=None,
        prompt_version="gh4_extract_v1",
        model="m",
        nonce="abc",
    )
    assert any(
        line.startswith("system[0] differs from prompt file gh4_extract_v1") for line in call.diff
    )
    assert "schema 'NoSuchSchema' is not registered" in call.diff
    assert call.fenced_chunks == 0
    assert not any("nonce" in line for line in call.diff)  # no fences: nothing to check


def test_a_matching_prompt_and_schema_block_are_clean() -> None:
    model = schema_model("DisclosurePolicyFields")
    assert model is not None
    request = _request(load_prompt("gh4_extract_v1").text, "goal text only")
    request["system"][1] = {"text": schema_block(model), "cache": True}
    call = check_call(
        None,  # type: ignore[arg-type]
        name="extract",
        request=request,
        response=None,
        prompt_version="gh4_extract_v1",
        model="m",
        nonce="abc",
    )
    assert call.diff == ()


def test_a_chat_call_without_a_schema_is_checked_for_its_prompt_only() -> None:
    request = {
        "model": "m",
        "system": [{"type": "text", "text": load_prompt("copilot_gather_v1").text}],
        "messages": [{"role": "user", "content": "Question from the tenant's operator: hi"}],
        "tools": ["search_evidence"],
    }
    call = check_call(
        None,  # type: ignore[arg-type]
        name="gather",
        request=request,
        response=None,
        prompt_version="copilot_gather_v1",
        model="m",
        nonce="abc",
    )
    assert call.diff == () and call.schema is None


def test_a_missing_prompt_version_is_reported() -> None:
    call = check_call(
        None,  # type: ignore[arg-type]
        name="x",
        request=_request("p", "u", schema=None),
        response=None,
        prompt_version=None,
        model="m",
        nonce=None,
    )
    assert call.diff == ("no prompt version recorded",)
