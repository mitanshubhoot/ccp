"""MT10 (A-153): ceilings per call type, and what leaves the context first."""

from __future__ import annotations

import pytest

from ccp.agent import core
from ccp.llm import context, release
from ccp.llm.context import Item


def test_every_call_type_has_its_starting_ceiling_in_the_bundle() -> None:
    limits = context.load()
    assert limits.of("tier2_extract") == 20_000
    assert limits.of("agent_first_call") == 15_000 and limits.of("agent_later_call") == 50_000
    assert limits.of("copilot_turn") == 30_000
    assert limits.of("document_haiku") == 90_000 and limits.of("document") == 200_000
    assert limits.swap_tokens == 5_000 and limits.preview_tokens == 500
    assert "context_ceilings.yaml" in release.CONFIG_FILES  # a change is a new bundle
    with pytest.raises(KeyError, match="no ceiling"):
        limits.of("anything_else")


def test_tokens_are_counted_the_same_way_everywhere() -> None:
    assert context.estimate_tokens("") == 0
    assert context.estimate_tokens("abcd") == 1 and context.estimate_tokens("abcde") == 2
    assert context.estimate_tokens({"a": 1}) == context.estimate_tokens('{"a": 1}')


def test_the_lowest_priority_leaves_first_and_a_pinned_item_never() -> None:
    items = [
        Item("instructions", 100, 0, pinned=True),
        Item("best", 50, 1),
        Item("cited", 50, 9, pinned=True),
        Item("middling", 50, 2),
        Item("worst", 50, 3),
    ]
    kept, dropped = context.fit(items, 210)
    assert kept == ["instructions", "best", "cited"] and dropped == ["middling", "worst"]
    assert context.fit(items, 1_000) == ([i.key for i in items], [])
    with pytest.raises(context.ContextCeilingExceeded):
        context.fit(items, 149)  # what must be sent is over the ceiling: nothing is sent


def test_a_previous_answer_pins_the_chunks_it_cites() -> None:
    fields = {
        "systems": [{"name": "GitHub", "citation": {"chunk_id": "c1", "quote": "q"}}],
        "review_date_citation": {"chunk_id": "c2", "quote": "q"},
        "readings": [{"findings": [{"citation": {"chunk_id": "span:e:1-9"}}]}],
    }
    assert core._cited_chunk_ids(fields) == {"c1", "c2", "span:e:1-9"}
    assert core._cited_chunk_ids(None) == set()
