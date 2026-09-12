from __future__ import annotations

import json

import pytest

from ccp.config import Settings
from ccp.observability import steps
from ccp.tenancy import TenantContext


def _lines(settings: Settings) -> list[dict[str, object]]:
    path = settings.logs_dir / "steps.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_step_writes_one_json_line_with_gen_ai_names(base_settings: Settings) -> None:
    tenant = TenantContext.for_id(base_settings, "acme")
    run_id = steps.new_run()
    with steps.step("unit.demo", layer="tests", tenant=tenant, inputs={"k": 1}) as s:
        s.model("claude-haiku-4-5", prompt_version="demo_v1")
        s.usage(input_tokens=10, output_tokens=2, cache_read=5)
        s.event("demo_event")
        s.set_output({"ok": True})
    line = _lines(base_settings)[-1]
    assert line["gen_ai.operation.name"] == "unit.demo"
    assert line["gen_ai.request.model"] == "claude-haiku-4-5"
    assert line["gen_ai.usage.input_tokens"] == 10
    assert line["gen_ai.usage.cache_read_input_tokens"] == 5
    assert line["ccp.run_id"] == run_id
    assert line["ccp.tenant_id"] == "acme"
    assert line["ccp.guardrail_events"] == ["demo_event"]
    assert line["output"] == {"ok": True}
    assert line["error"] is None
    assert isinstance(line["latency_ms"], float)


def test_nested_steps_record_parent(base_settings: Settings) -> None:
    with steps.step("outer", layer="tests") as outer, steps.step("inner", layer="tests"):
        pass
    inner, outer_line = _lines(base_settings)[-2:]
    assert inner["gen_ai.operation.name"] == "inner"
    assert inner["ccp.parent_step_id"] == outer.step_id
    assert outer_line["ccp.parent_step_id"] is None


def test_step_records_error_and_reraises(base_settings: Settings) -> None:
    with pytest.raises(ValueError), steps.step("boom", layer="tests"):
        raise ValueError("bad input")
    line = _lines(base_settings)[-1]
    assert line["error"] == "ValueError: bad input"
