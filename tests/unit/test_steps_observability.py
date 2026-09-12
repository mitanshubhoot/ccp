"""The step logger's Phase 16 duties: blob offload, cost, sink emission with nesting, the
Langfuse-unreachable degrade path, and a sink that fails mid-run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from ccp.config import Settings
from ccp.observability import steps, tracing
from ccp.observability.tracing import FailingSink, RecordingSink


@pytest.fixture
def log(tmp_path: Path) -> Path:
    settings = Settings(ccp_data_dir=tmp_path)
    steps.configure(settings, sink=None)
    yield settings.logs_dir / "steps.jsonl"  # type: ignore[misc]
    steps.set_sink(None)


def _lines(log: Path) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_large_inputs_and_outputs_are_offloaded_to_blobs(log: Path) -> None:
    big = {"text": "x" * 10_000}
    with steps.step("blob.test", layer="tests", inputs=big) as s:
        s.set_output({"payload": "y" * 6000, "n": 1})
    line = _lines(log)[-1]
    assert line["inputs"]["$blob"].startswith("blobs/") and line["inputs"]["bytes"] > 4096
    assert line["output"]["$blob"].endswith(".output.json")
    assert (log.parent / line["inputs"]["$blob"]).exists()
    resolved = steps.resolve_blobs(line, log)
    assert resolved["inputs"] == big and resolved["output"]["n"] == 1
    assert list(steps.read_lines(log))[-1]["output"]["payload"] == "y" * 6000
    with steps.step("small.test", layer="tests", inputs={"a": 1}) as s:
        s.set_output({"b": 2})
    assert _lines(log)[-1]["inputs"] == {"a": 1}  # small values stay inline


def test_model_steps_carry_cost_and_prompt_hash(log: Path) -> None:
    with steps.step("tier2.extract", layer="tests", kind="generation") as s:
        s.model("claude-haiku-4-5", prompt_version="gh4_extract_v1", prompt_sha256="abc")
        s.usage(input_tokens=1000, output_tokens=100, cache_read=200, cache_write=0)
    line = _lines(log)[-1]
    assert line["gen_ai.request.model"] == "claude-haiku-4-5"
    assert line["ccp.prompt_sha256"] == "abc" and line["ccp.prompt_version"] == "gh4_extract_v1"
    assert abs(line["ccp.cost_usd"] - (1000 * 1.0 + 100 * 5.0 + 200 * 0.10) / 1e6) < 1e-9
    assert line["ccp.cost_basis"] == "anthropic_list"
    with steps.step("plain", layer="tests"):
        pass
    assert _lines(log)[-1]["ccp.cost_usd"] is None and _lines(log)[-1]["ccp.cost_basis"] is None


def test_sink_receives_nested_spans_with_one_trace_per_run(log: Path) -> None:
    sink = RecordingSink()
    steps.set_sink(sink)
    run_id = steps.new_run()
    with steps.step("outer", layer="tests", inputs={"q": 1}) as outer:
        assert steps.graph_callbacks() == []  # the recording sink offers none
        with steps.step("inner", layer="tests", kind="generation") as inner:
            inner.model("claude-haiku-4-5")
            inner.usage(input_tokens=10, output_tokens=5)
            inner.set_output({"ok": True})
        outer.set_output({"done": True})
    assert [s["name"] for s in sink.spans] == ["outer", "inner"]
    outer_span, inner_span = sink.spans
    assert inner_span["parent_key"] == outer.step_id and outer_span["parent_key"] is None
    assert outer_span["trace_key"] == inner_span["trace_key"] == tracing.trace_key_for(run_id)
    assert len(outer_span["trace_key"]) == 32
    assert inner_span["ended"] and inner_span["model"] == "claude-haiku-4-5"
    assert inner_span["usage"]["input_tokens"] == 10 and inner_span["cost_usd"] > 0
    assert outer_span["output"] == {"done": True} and outer_span["error"] is None
    with pytest.raises(ValueError):
        with steps.step("boom", layer="tests"):
            raise ValueError("no")
    assert sink.spans[-1]["error"] == "ValueError: no"
    steps.flush()
    assert sink.flushed == 1


def test_unreachable_langfuse_warns_once_and_keeps_logging(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(
        ccp_data_dir=tmp_path,
        langfuse_public_key=SecretStr("pk-test"),
        langfuse_secret_key=SecretStr("sk-test"),
        langfuse_host="http://127.0.0.1:9",
    )

    def probe(host: str) -> None:
        raise ConnectionError(f"cannot reach {host}")

    warning = steps.configure(settings, probe=probe)
    assert warning is not None and "unreachable" in warning and not steps.sink_active()
    with steps.step("still.logs", layer="tests") as s:
        s.set_output({"ok": True})
    lines = _lines(settings.logs_dir / "steps.jsonl")
    assert lines[-1]["gen_ai.operation.name"] == "still.logs"
    assert capsys.readouterr().err.count("unreachable") == 1
    steps.set_sink(None)


def test_no_keys_means_no_sink_and_no_warning(tmp_path: Path) -> None:
    assert steps.configure(Settings(ccp_data_dir=tmp_path)) is None
    assert not steps.sink_active()


def test_a_failing_sink_is_disabled_without_losing_the_step(log: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    steps.set_sink(FailingSink())
    with steps.step("survives", layer="tests") as s:
        s.set_output({"ok": True})
    assert _lines(log)[-1]["output"] == {"ok": True}
    assert not steps.sink_active()
    assert "sink failed" in capsys.readouterr().err


def test_build_sink_without_keys() -> None:
    assert tracing.build_sink(Settings()) == (None, None)
