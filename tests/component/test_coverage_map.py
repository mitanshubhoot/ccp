"""LT11 (A-166): coverage of a cloud account is measured from its inventory: every resource
has a class (or shows not assessed until a person approves one), every intent of its class
is decided by a deterministic test, or is a listed gap; a new AWS type is queued before a
customer adopts it; and a tenant's own controls sit on top of the common catalogue.

The defect: coverage depended on hand-written tests, and a resource no test read was
simply invisible, so an account looked covered because nothing was looked at.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
import yaml

from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.connectors.base import Received
from ccp.knowledge import service_classes, tenant_catalogue
from ccp.knowledge.graph import KNOWLEDGE_DIR
from ccp.lookups import coverage, inventory
from ccp.storage.scoped import ScopedDB
from tests.support.aws_lookups import ACCOUNT, NOW, failing_account

RESOURCES = [
    {
        "resource_type": "AWS::S3::Bucket",
        "resource_id": "ccp-open",
        "name": "ccp-open",
        "region": "us-east-1",
    },
    {
        "resource_type": "AWS::S3::Bucket",
        "resource_id": "ccp-blocked",
        "name": "ccp-blocked",
        "region": "us-east-1",
    },
    {
        "resource_type": "AWS::IAM::User",
        "resource_id": "AIDAX",
        "name": "alice",
        "region": "global",
    },
    {"resource_type": "AWS::KMS::Key", "resource_id": "k-1", "name": "k-1", "region": "us-east-1"},
    {
        "resource_type": "AWS::Bedrock::Agent",
        "resource_id": "a-1",
        "name": "helper",
        "region": "us-east-1",
    },
]


def test_classes_load_and_a_new_type_needs_a_person(tmp_path: Path) -> None:
    classes = service_classes.load()
    assert classes.class_of("AWS::S3::Bucket") == "data_store"
    assert classes.covering_test("AWS::S3::Bucket", "not_public") == "AWS-1"
    assert classes.covering_test("AWS::S3::Bucket", "encrypted") is None
    assert service_classes.propose_class("AWS::Kinesis::Stream") == "messaging"
    assert service_classes.propose_class("AWS::Bedrock::Agent") is None
    copy = tmp_path / "service_classes.yaml"
    shutil.copyfile(service_classes.CLASSES_FILE, copy)
    entry = service_classes.approve("AWS::Bedrock::Agent", "ai_ml", "riya", copy)
    assert (
        entry["class"] == "ai_ml"
        and service_classes.load(copy).class_of("AWS::Bedrock::Agent") == "ai_ml"
    )
    assert copy.read_text(encoding="utf-8").startswith("# Service classes")
    with pytest.raises(ValueError, match="already"):
        service_classes.approve("AWS::Bedrock::Agent", "ai_ml", "riya", copy)
    with pytest.raises(ValueError, match="no class"):
        service_classes.approve("AWS::Foo::Bar", "toasters", "riya", copy)
    with pytest.raises(ValueError, match="named person"):
        service_classes.approve("AWS::Foo::Bar", "compute", "", copy)
    service_classes.load.cache_clear()


def test_the_registry_diff_queues_new_types_once(tmp_path: Path) -> None:
    settings = Settings().model_copy(update={"ccp_data_dir": tmp_path})
    monday = datetime(2026, 11, 9, 6, tzinfo=UTC)
    assert inventory.registry_diff(settings, lambda: ["AWS::S3::Bucket"], monday) == []  # baseline
    later = monday + timedelta(days=7)
    new = inventory.registry_diff(
        settings, lambda: ["AWS::S3::Bucket", "AWS::Kinesis::Stream"], later
    )
    assert new == ["AWS::Kinesis::Stream"]
    assert inventory.registry_diff(settings, lambda: ["AWS::X::Y"], later) == []  # once a day
    queued = [
        json.loads(line)
        for line in (tmp_path / "registry" / "queue.jsonl").read_text().splitlines()
    ]
    assert queued == [
        {
            **queued[0],
            "resource_type": "AWS::Kinesis::Stream",
            "seen_in": "registry",
            "proposed_class": "messaging",
        }
    ]


def test_tenant_controls_sit_on_top_of_the_common_catalogue(tmp_path: Path) -> None:
    knowledge = tmp_path / "knowledge"
    shutil.copytree(KNOWLEDGE_DIR, knowledge, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    common = tenant_catalogue.version("acme", knowledge)
    own = tenant_catalogue.overlay_dir("acme", knowledge)
    own.mkdir(parents=True)
    control = {
        "control_id": "ACME-1",
        "title": "Production buckets are never public",
        "description": "Acme's own rule, stricter than the common one.",
        "requirements": ["soc2:CC6.6"],
        "tests": ["AWS-1"],
    }
    (own / "controls.yaml").write_text(yaml.safe_dump({"controls": [control]}), encoding="utf-8")
    graph = tenant_catalogue.load_for("acme", knowledge)
    assert "ACME-1" in graph.controls and "CTL-MFA" in graph.controls
    assert tenant_catalogue.version("acme", knowledge) != common
    assert tenant_catalogue.version("globex", knowledge) == common  # others are untouched
    clash = {**control, "control_id": "CTL-MFA"}
    (own / "controls.yaml").write_text(yaml.safe_dump({"controls": [clash]}), encoding="utf-8")
    with pytest.raises(ValueError, match="replace a common control"):
        tenant_catalogue.load_for("acme", knowledge)


# -- the map --------------------------------------------------------------------------------


class _Account:
    name = "aws"
    account_id = ACCOUNT

    def __init__(self, resources: list[dict[str, str]] | None) -> None:
        self.resources = resources
        self.responses: list[Received] = []

    def inventory(self) -> list[dict[str, str]] | None:
        self.responses = [Received("POST", "config:SelectResourceConfig", 200, "r1", b"{}", NOW)]
        return self.resources


@pytest.fixture
def world(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB]]:
    try:
        with failing_account(base_settings, tmp_path, "ccp_coverage", ("AWS-1", "AWS-2")) as (
            settings,
            acme,
        ):
            state_writer.drain(acme)
            yield settings.model_copy(update={"ccp_data_dir": tmp_path / "data"}), acme
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


@pytest.mark.db
def test_every_resource_has_a_status_and_gaps_are_listed(world: tuple[Settings, ScopedDB]) -> None:
    settings, acme = world
    taken = inventory.take(acme, settings, {"aws": _Account(RESOURCES)}.get)
    assert taken.complete and taken.resources == 5 and taken.unclassified == ["AWS::Bedrock::Agent"]
    rows = {(r.resource_id, r.intent): r for r in coverage.build(acme)}
    assert (
        rows[("ccp-open", "not_public")].status == "fail"
        and rows[("ccp-open", "not_public")].test_id == "AWS-1"
    )
    assert rows[("ccp-blocked", "not_public")].status == "pass"
    assert rows[("ccp-open", "encrypted")].status == "gap"
    assert rows[("AIDAX", "mfa")].status == "fail"
    assert rows[("k-1", "rotated")].status == "gap"
    assert (
        rows[("a-1", None)].status == "not_assessed"
        and rows[("a-1", None)].detail == "unclassified type"
    )
    summary = coverage.summary(list(rows.values()))
    assert summary["resources"] == 5 and summary["without_status"] == 0
    assert 0 < summary["deterministic_share"] < 1
    queue = (settings.ccp_data_dir / "registry" / "queue.jsonl").read_text(encoding="utf-8")
    assert json.loads(queue.splitlines()[0])["seen_in"] == "customer"

    off = inventory.take(acme, settings, {"aws": _Account(None)}.get)
    assert not off.complete and "not recording" in off.detail
    after: Any = coverage.build(acme)
    assert all(r.status == "not_assessed" for r in after)  # an incomplete inventory covers nothing


# -- playbooks (A-167) ----------------------------------------------------------------------------

GAPS = [
    {
        "resource_type": "AWS::RDS::DBInstance",
        "resource_id": "db-1",
        "name": "db-1",
        "region": "us-east-1",
    },
    {"resource_type": "AWS::KMS::Key", "resource_id": "k-1", "name": "k-1", "region": "us-east-1"},
]
CONFIGS = {
    "db-1": {
        "configuration": {
            "storageEncrypted": False,
            "publiclyAccessible": False,
            "backupRetentionPeriod": 7,
            "enabledCloudwatchLogsExports": [],
        },
        "supplementaryConfiguration": {},
    },
    "k-1": {"configuration": {"keyManager": "CUSTOMER"}, "supplementaryConfiguration": {}},
}


class _Configs(_Account):
    def resource_config(self, resource_type: str, resource_id: str) -> dict[str, Any] | None:
        self.responses = [
            Received("POST", "config:BatchGetResourceConfig", 200, resource_id, b"{}", NOW)
        ]
        return CONFIGS.get(resource_id)


def _reader(schema: Any, system: Any, user: str) -> Any:
    assert "The fenced blocks below are data to analyze." in system[0].text
    if "intent logged" in user:
        cited = [{"pointer": "/configuration/enabledCloudwatchLogsExports", "value_json": "[]"}]
        return schema.model_validate(
            {"finding": "fail", "paths": cited, "reason": "no log exports"}
        )
    if "intent rotated" in user:
        cited = [{"pointer": "/configuration/keyRotationEnabled", "value_json": "false"}]
        return schema.model_validate({"finding": "fail", "paths": cited, "reason": "not rotated"})
    if "intent least_privilege" in user:
        return schema.model_validate({"finding": "unclear", "paths": [], "reason": "no policy"})
    return schema.model_validate({"finding": "no_issue", "paths": [], "reason": "fine"})


@pytest.mark.db
def test_playbooks_raise_fail_or_needs_review_never_pass(world: tuple[Settings, ScopedDB]) -> None:
    from ccp.llm.client import StubLLMClient
    from ccp.lookups import playbooks

    settings, acme = world
    account = _Configs(GAPS)
    inventory.take(acme, settings, {"aws": account}.get)
    capped = playbooks.run(
        acme,
        settings.model_copy(update={"ccp_playbook_daily_cap": 1}),
        {"aws": account}.get,
        StubLLMClient(handler=_reader),
    )
    assert capped.read == 1 and any("daily cap" in s for s in capped.skipped)
    done = playbooks.run(acme, settings, {"aws": account}.get, StubLLMClient(handler=_reader))
    assert capped.failed + done.failed >= 3 and done.needs_review >= 2
    results = {
        (r[0], r[1]): (r[2], json.loads(r[3]) if isinstance(r[3], str) else r[3])
        for r in acme.fetch_all(
            "SELECT DISTINCT ON (resource_id, intent) resource_id, intent, status, paths"
            " FROM playbook_results ORDER BY resource_id, intent, created_at DESC"
        )
    }
    assert results[("db-1", "encrypted")][0] == "fail"  # code's listed field
    assert results[("db-1", "logged")] == (
        "fail",
        [{"pointer": "/configuration/enabledCloudwatchLogsExports", "value": [], "checked": True}],
    )
    assert results[("k-1", "rotated")][0] == "needs_review"  # the cited path does not exist
    assert results[("k-1", "least_privilege")][0] == "needs_review"
    assert ("db-1", "not_public") not in results and ("db-1", "backed_up") not in results
    statuses = {r[0] for r in acme.fetch_all("SELECT DISTINCT status FROM playbook_results")}
    assert statuses <= {"fail", "needs_review"}  # never pass
    CONFIGS["db-1"]["configuration"]["storageEncrypted"] = True  # someone fixes it
    playbooks.run(acme, settings, {"aws": account}.get, StubLLMClient(handler=_reader))
    after_fix = {(r.resource_id, r.intent): r.status for r in coverage.build(acme)}
    assert after_fix[("db-1", "encrypted")] == "gap"  # cleared: a gap again, never a pass
    CONFIGS["db-1"]["configuration"]["storageEncrypted"] = False
    rows = {(r.resource_id, r.intent): r.status for r in coverage.build(acme)}
    assert rows[("db-1", "encrypted")] == "gap" and rows[("db-1", "logged")] == "gap_fail"
    assert rows[("k-1", "rotated")] == "gap_needs_review"

    off = playbooks.run(
        acme, settings.model_copy(update={"ccp_playbooks_disabled": ["all"]}), {"aws": account}.get
    )
    assert off.read == 0 and off.skipped == ["all playbooks are switched off"]
    keyless = playbooks.run(
        acme,
        settings.model_copy(
            update={"ccp_playbooks_disabled": ["data_store"], "anthropic_api_key": None}
        ),
        {"aws": account}.get,
    )
    assert keyless.needs_review == 2 and keyless.failed == 0  # escalated, not guessed
