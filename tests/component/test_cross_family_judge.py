"""NT8 (Decision 9, spec.md 5.3, A-103): a judge from another model family grades fixture
cases only and never a case derived from customer data; content-free outputs fail in code
(negative controls); the judge is shown the spans code verified; and reviewer labels become
labelled-set intake outside the repository. The cross-family client runs against a recorded
Chat Completions API (httpx.MockTransport), so no network or key is needed."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr

from ccp.agent import decisions
from ccp.approvals import store as approvals
from ccp.config import REPO_ROOT, Settings
from ccp.evals import calibration, labels, runner
from ccp.evals import judge as judging
from ccp.evals.schema import GoldenCase
from ccp.llm.budget import BudgetTracker, RunBudget
from ccp.llm.client import StubLLMClient, SystemBlock
from ccp.llm.errors import RetryableModelError, TerminalModelError, UnpricedModel
from ccp.llm.openai_judge import CrossFamilyJudgeClient
from ccp.observability import cost
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext
from tests.support.pipeline import prepared_tenants
from tests.support.stub_judge import lenient_judge

OTHER = "gpt-fixture-judge"  # a cross-family judge id, priced for the test only


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(cost.PRICES, OTHER, cost.Price(input=1.0, output=4.0, cache_read=0.1))


def _tracker() -> BudgetTracker:
    return BudgetTracker(RunBudget(max_tokens=10**6, max_usd=10.0, max_seconds=600))


def _completion(content: str, **over: Any) -> dict[str, Any]:
    return {
        "model": OTHER,
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "refusal": None},
                "finish_reason": over.pop("finish_reason", "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "prompt_tokens_details": {"cached_tokens": 200},
        },
        **over,
    }


def _judge_client(replies: list[httpx.Response]) -> tuple[CrossFamilyJudgeClient, list[Any]]:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return replies.pop(0)

    client = CrossFamilyJudgeClient(
        "sk-test", transport=httpx.MockTransport(handler), sleep=lambda s: None
    )
    return client, seen


DRAFT = {"criteria": [{"number": 1, "met": True, "evidence": "quoted"}], "overall_pass": True}


def test_the_cross_family_judge_asks_for_a_strict_schema_and_redacts(priced: None) -> None:
    client, seen = _judge_client([httpx.Response(200, json=_completion(json.dumps(DRAFT)))])
    secret = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    out = client.extract(
        schema=judging.JudgeDraft,
        system=[SystemBlock("grade it")],
        user=f"candidate mentions {secret}",
        model=OTHER,
        tracker=_tracker(),
    )
    body = seen[0]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert secret not in json.dumps(body) and "temperature" not in body
    assert isinstance(out.parsed, judging.JudgeDraft) and out.parsed.overall_pass
    assert out.usage.input_tokens == 1000 and out.usage.cache_read_input_tokens == 200
    assert out.usage.usd == pytest.approx(
        cost.estimate_usd(OTHER, input_tokens=1000, output_tokens=80, cache_read_tokens=200)
    )


def test_the_cross_family_judge_retries_re_asks_and_stops_honestly(priced: None) -> None:
    client, seen = _judge_client(
        [
            httpx.Response(429, json={"error": "slow down"}),
            httpx.Response(200, json=_completion("not json")),
            httpx.Response(200, json=_completion(json.dumps(DRAFT))),
        ]
    )
    out = client.extract(
        schema=judging.JudgeDraft, system=[], user="u", model=OTHER, tracker=_tracker()
    )
    assert out.parsed.overall_pass and len(seen) == 3
    assert seen[2]["messages"][1]["content"].endswith("every required field present.")
    refusing = _completion("")
    refusing["choices"][0]["message"]["refusal"] = "I can't help with that."
    for reply, why in (
        (refusing, "refused"),
        (_completion("{", finish_reason="length"), "cut off"),
    ):
        client, _ = _judge_client([httpx.Response(200, json=reply)])
        with pytest.raises(TerminalModelError, match=why):
            client.extract(
                schema=judging.JudgeDraft, system=[], user="u", model=OTHER, tracker=_tracker()
            )
    client, _ = _judge_client([httpx.Response(503, text="busy")] * 3)
    with pytest.raises(RetryableModelError):
        client.extract(
            schema=judging.JudgeDraft, system=[], user="u", model=OTHER, tracker=_tracker()
        )


def test_the_cross_family_judge_refuses_unpriced_and_claude_models_before_any_call() -> None:
    client, seen = _judge_client([])
    with pytest.raises(UnpricedModel):
        client.extract(
            schema=judging.JudgeDraft, system=[], user="u", model="gpt-unpriced", tracker=_tracker()
        )
    with pytest.raises(TerminalModelError, match="Claude model"):
        client.extract(
            schema=judging.JudgeDraft,
            system=[],
            user="u",
            model="claude-opus-5",
            tracker=_tracker(),
        )
    with pytest.raises(TerminalModelError, match="only grades"):
        client.complete(system=[], user="u", model=OTHER, tracker=_tracker())
    assert seen == []


def _rubric() -> judging.Rubric:
    return judging.load_rubric("verdict_reasoning")


def test_a_cross_family_judge_never_grades_customer_data() -> None:
    stub = StubLLMClient(handler=lenient_judge)
    with pytest.raises(judging.DataBoundary):
        judging.judge_consistently(
            stub,
            _rubric(),
            case_input={},
            output={"status": "pass", "reasoning": "fine"},
            model=OTHER,
            tracker=_tracker(),
        )
    assert stub.calls == []
    graded = judging.judge_consistently(
        stub,
        _rubric(),
        case_input={},
        output={"status": "pass", "reasoning": "SECURITY.md states a 5-day response time."},
        model=OTHER,
        tracker=_tracker(),
        runs=1,
        customer_data=False,
    )
    assert graded.rubric == "verdict_reasoning" and stub.calls


def test_the_runner_routes_fixture_cases_to_the_cross_family_judge_only(
    base_settings: Settings,
) -> None:
    settings = base_settings.model_copy(update={"ccp_fixture_judge_model": OTHER})
    fixture = GoldenCase(
        id="j-fixture",
        level="component",
        difficulty="easy",
        source="synthetic",
        target="knowledge_router",
        expected={},
        grading="judge",
        rubric="verdict_reasoning",
    )
    captured = fixture.model_copy(update={"id": "j-captured", "tags": ["captured"]})
    customer = fixture.model_copy(update={"id": "j-customer", "origin": "customer"})
    assert runner.judge_model_for(settings, fixture) == OTHER
    assert runner.judge_model_for(settings, captured) == settings.ccp_judge_model
    assert runner.judge_model_for(settings, customer) == settings.ccp_judge_model
    # the cross-family judge needs its own key; a keyless fixture case is skipped, not graded
    keyless = settings.model_copy(update={"openai_api_key": None})
    assert runner.missing_providers(keyless, fixture) == ["openai"]
    keyed = settings.model_copy(update={"openai_api_key": SecretStr("sk-test")})
    assert runner.missing_providers(keyed, fixture) == []


def test_calibration_refuses_a_cross_family_judge_on_reviewer_labels() -> None:
    case = calibration.LabeledCase(
        id="rev-1",
        kind="verdict",
        category="reviewer_approved",
        rubric="verdict_reasoning",
        input={},
        output={"status": "pass", "reasoning": "r"},
        labels={},
        overall=True,
        origin="customer",
    )
    stub = StubLLMClient(handler=lenient_judge)
    with pytest.raises(judging.DataBoundary):
        calibration.calibrate(stub, [case], model=OTHER, tracker=_tracker())
    assert stub.calls == []


@pytest.mark.parametrize(
    "output",
    [
        None,
        "",
        ":",
        "...",
        "Thought process:",
        {"status": "pass", "reasoning": ""},
        {"answer": "  -- "},
        {},
    ],
)
def test_content_free_outputs_fail_without_a_model_call(output: Any) -> None:
    stub = StubLLMClient(handler=lenient_judge)
    result = judging.judge(
        stub, _rubric(), case_input={}, output=output, model="claude-opus-5", tracker=_tracker()
    )
    assert not result.overall_pass and not any(c.met for c in result.criteria)
    assert result.guardrail_events == [judging.NEGATIVE_CONTROL] and stub.calls == []


def test_the_judge_is_shown_the_verified_spans_apart_from_the_reasoning() -> None:
    prompts: list[str] = []

    def handler(schema: type[Any], system: list[SystemBlock], user: str) -> Any:
        prompts.append(user)
        return lenient_judge(schema, system, user)

    output = {
        "status": "pass",
        "reasoning": "The policy says reports are acknowledged within 5 business days.",
        "citations": [
            {
                "chunk_id": "c1",
                "quote": "We acknowledge every report within 5 business days.",
                "verified": True,
            },
            {"chunk_id": "c2", "quote": "Invented line the model made up.", "verified": False},
        ],
    }
    judging.judge(
        StubLLMClient(handler=handler),
        _rubric(),
        case_input={},
        output=output,
        model="claude-opus-5",
        tracker=_tracker(),
    )
    user = prompts[0]
    spans_at = user.index("code_verified_spans")
    assert "- We acknowledge every report within 5 business days." in user[spans_at:]
    assert "- Invented line the model made up." not in user
    assert "not proof of what it did" in user


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_nt8_labels", ("acme",)) as (settings, dbs):
        yield settings, dbs["acme"]


def test_reviewer_labels_become_intake_outside_the_repository(
    world: tuple[Settings, ScopedDB], tmp_path: Path
) -> None:
    _, acme = world
    opted = ScopedDB(
        acme._pool,  # noqa: SLF001 - the same pool, a tenant that opted in to eval use
        TenantContext(
            acme.tenant.tenant_id, acme.tenant.settings.model_copy(update={"label_consent": "eval"})
        ),
    )
    draft = {"status": "pass", "reasoning": "MFA enforced for the org.", "confidence": 0.8}
    approved = approvals.open_item(opted, "tier3_interrupt", "thread-a", {"draft": draft})
    approvals.record_event(
        opted, approved, "approved", "lead", "ok", decision=decisions.build("approve", "lead")
    )
    overridden = approvals.open_item(opted, "tier3_interrupt", "thread-b", {"draft": draft})
    approvals.record_event(
        opted,
        overridden,
        "approved",
        "lead",
        "wrong",
        decision=decisions.build("override", "lead", status="fail"),
    )
    other = approvals.open_item(opted, "policy_draft", "draft-z", {"path": "drafts/z.md"})
    approvals.record_event(opted, other, "approved", "cto", "fine")
    unconsented = approvals.open_item(acme, "tier3_interrupt", "thread-c", {"draft": draft})
    approvals.record_event(
        acme, unconsented, "approved", "lead", "ok", decision=decisions.build("approve", "lead")
    )

    out = tmp_path / "intake" / "acme-labels.yaml"
    written, skipped = labels.export(opted, out)
    cases = yaml.safe_load(out.read_text(encoding="utf-8"))
    by_ref = {c["input"]["ref_id"]: c for c in cases}
    assert written == len(cases) and "thread-c" not in by_ref  # no consent, no export
    assert by_ref["thread-a"]["overall"] is True and by_ref["thread-a"]["output"] == draft
    assert by_ref["thread-b"]["overall"] is False and "status: fail" in by_ref["thread-b"]["note"]
    assert all(c["origin"] == "customer" and c["labels"] == {} for c in cases)
    assert skipped.get("kind policy_draft", 0) >= 1
    calibration.LabeledCase.model_validate(by_ref["thread-a"])  # the labelled-set format
    with pytest.raises(labels.InsideRepository):
        labels.export(opted, REPO_ROOT / "evals" / "labeled" / "intake.yaml")


def test_the_runner_runs_a_judged_case_k_times_and_fails_it_on_one_bad_run(
    base_settings: Settings,
) -> None:
    cases, hashes = runner.load_cases(base_settings.ccp_golden_dir)
    plain = next(c for c in cases if c.target == "knowledge_router")
    judged = plain.model_copy(
        update={"id": "judged-k", "grading": "judge", "rubric": "verdict_reasoning"}
    )
    votes = iter([True, False, True])

    def flipping(schema: type[Any], system: list[SystemBlock], user: str) -> Any:
        ok = next(votes)
        criteria = [{"number": n, "met": ok, "evidence": "x"} for n in range(1, 6)]
        return schema.model_validate({"criteria": criteria, "overall_pass": ok})

    settings = base_settings.model_copy(
        update={"ccp_judge_model": "claude-opus-5", "ccp_judge_runs": 1}
    )
    report = runner.run(
        settings, [plain, judged], hashes, judge_llm=StubLLMClient(handler=flipping), runs=3
    )
    by_id = {r.id: r for r in report.cases}
    assert by_id[plain.id].runs == 1 and by_id[plain.id].passed  # deterministic: once
    folded = by_id["judged-k"]
    assert folded.model_backed and folded.runs == 3 and folded.run_passes == 2
    assert not folded.passed  # pass^3 needs every run
    assert report.metrics is not None
    stats = report.metrics.ai_tests["knowledge_router"]
    assert stats.pass_k.estimate == 0.0 and stats.pass_at_1.estimate == pytest.approx(2 / 3, 1e-3)
