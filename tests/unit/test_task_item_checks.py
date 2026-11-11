"""MT10 (A-151): the code checks between an item run and its merge, and the fan-out gate."""

from __future__ import annotations

import json
from pathlib import Path

from ccp.agent.allowlists import ALLOWLISTS
from ccp.agent.graphs.task_item import check_readings, tokens_of
from ccp.agent.tools import TOOL_SPECS
from ccp.config import Settings
from ccp.evaluators.extraction.task_item import RESULT_TOKEN_CAP
from ccp.tasks import launcher

CHUNKS = {
    "c1": {
        "chunk_id": "c1",
        "evidence_entity_id": "manual:evidence:policy",
        "text": "Section 3. Multi-factor authentication is required for every human identity.",
        "start_offset": 1000,
    },
    "c2": {"chunk_id": "c2", "evidence_entity_id": "e2", "text": "No offsets here."},
}


def _reading(item_id: str, *findings: tuple[str, str], answer: str = "yes") -> dict:  # type: ignore[type-arg]
    return {
        "item_id": item_id,
        "answer": answer,
        "reason": "r",
        "findings": [
            {"statement": f"s{n}", "citation": {"chunk_id": c, "quote": q}}
            for n, (c, q) in enumerate(findings)
        ],
        "missing": [],
    }


def test_a_verified_claim_becomes_a_pointer_and_a_false_one_is_dropped() -> None:
    fields = {
        "readings": [
            _reading(
                "a",
                ("c1", "Multi-factor authentication is required"),
                ("c1", "Passwords are enough"),
                ("c9", "anything"),
                ("c2", "No offsets here."),
            )
        ]
    }
    readings, rejections, events = check_readings(fields, ["a"], CHUNKS)
    assert rejections == []
    first, second = readings["a"]["findings"]
    assert first["start_offset"] == 1000 + len("Section 3. ")
    assert first["end_offset"] - first["start_offset"] == len(
        "Multi-factor authentication is required"
    )
    assert first["evidence_entity_id"] == "manual:evidence:policy"
    assert second["start_offset"] is None and second["chunk_id"] == "c2"  # cited, place unknown
    assert readings["a"]["dropped_claims"] == 2
    assert "citation_quote_not_found:a:c1" in events and "citation_unknown_chunk:a:c9" in events


def test_readings_for_other_items_or_twice_are_merge_rejections() -> None:
    fields = {"readings": [_reading("a"), _reading("a"), _reading("zz")]}
    readings, rejections, events = check_readings(fields, ["a", "b"], CHUNKS)
    assert list(readings) == ["a"]
    assert rejections == ["duplicate_reading:a", "unknown_item:zz"]
    assert "no_reading:b" in events


def test_a_reading_is_held_to_its_token_ceiling() -> None:
    """The schema's own limits allow about 1,750 tokens; code holds a reading to 1,500."""
    long_text = " ".join(f"clause {n} requires a named owner and a review" for n in range(40))
    chunks = {**CHUNKS, "c3": {"chunk_id": "c3", "evidence_entity_id": "e3", "text": long_text}}
    quote = long_text[:290]
    big = _reading("a", *[("c3", quote)] * 8)
    big["reason"] = "r" * 300
    big["missing"] = ["m" * 200] * 5
    for f in big["findings"]:
        f["statement"] = "x" * 300
    readings, _, events = check_readings({"readings": [big]}, ["a"], chunks)
    assert tokens_of(readings["a"]) <= RESULT_TOKEN_CAP
    assert 0 < len(readings["a"]["findings"]) < 8 and "result_trimmed:a" in events


def test_task_item_runs_read_and_cannot_start_runs() -> None:
    tools = ALLOWLISTS["task_item"]
    assert {TOOL_SPECS[t].cls for t in tools} == {"read"}
    assert not any(word in t for t in TOOL_SPECS for word in ("spawn", "launch", "enqueue"))
    assert not {"get_access_grants", "list_findings"} & tools


def test_fanout_needs_the_config_and_a_single_run_baseline(tmp_path: Path) -> None:
    settings = Settings().model_copy(update={"ccp_eval_runs_dir": tmp_path})
    config = launcher.load_kinds()
    assert config.fanout == {"audit_prep": True, "questionnaire": True}
    assert config.single_run_items == 10
    assert not launcher.fanout_enabled(settings, "questionnaire", config)
    (tmp_path / "task_measure-questionnaire-fanout-20260101T000000.json").write_text("{}")
    assert not launcher.fanout_enabled(settings, "questionnaire", config)  # not a baseline
    (tmp_path / "task_measure-questionnaire-single-20260101T000000.json").write_text(
        json.dumps({"accuracy": 0.7})
    )
    assert launcher.fanout_enabled(settings, "questionnaire", config)
    off = launcher.KindsConfig(10, {"questionnaire": False})
    assert not launcher.fanout_enabled(settings, "questionnaire", off)


def test_the_hourly_job_and_the_item_worker_are_registered() -> None:
    from ccp.evaluators import jobs
    from ccp.ingest import topics, worker

    assert "tasks" in jobs.JOBS and "task_items" in worker.CONSUMERS
    assert topics.kind_of("task_items") is topics.TopicKind.parallel
    assert "task_items" in jobs.UNPARKED  # released with the budget's day, like the others
