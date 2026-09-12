"""Table-driven mapper tests: one fixture event of every type in, exact entities out."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from ccp.config import Settings
from ccp.connectors.base import SourceEvent
from ccp.connectors.registry import build_connector
from ccp.normalize.consumer import map_event
from ccp.normalize.entities import AccessGrant, Asset, Evidence, Person, Vendor
from ccp.normalize.github_mapper import account_kind_of
from ccp.normalize.manual_mapper import map_manual_event
from ccp.tenancy import TenantContext, TenantId
from ccp.types import AccountKind, EmploymentStatus, EvidenceType, GrantState


def _fixture_event(
    settings: Settings, tenant: str, connector: str, source_event_id: str
) -> SourceEvent:
    conn = build_connector(connector, settings, "fixture")  # type: ignore[arg-type]
    for event in conn.pull(TenantContext.for_id(settings, tenant)):
        if event.source_event_id == source_event_id:
            return event
    raise LookupError(source_event_id)


def _all_event_types(settings: Settings, tenant: str, connector: str) -> set[str]:
    conn = build_connector(connector, settings, "fixture")  # type: ignore[arg-type]
    return {e.event_type for e in conn.pull(TenantContext.for_id(settings, tenant))}


@pytest.mark.parametrize("connector", ["github", "hris", "manual"])
def test_every_fixture_event_type_maps_without_guardrail_surprises(
    base_settings: Settings, connector: str
) -> None:
    """Coverage: every event type in both tenants' fixtures maps to at least one entity."""
    for tenant in ("acme", "globex"):
        conn = build_connector(connector, base_settings, "fixture")  # type: ignore[arg-type]
        for event in conn.pull(TenantContext.for_id(base_settings, tenant)):
            result = map_event(event)
            assert result.entities, f"{event.event_type} produced no entities"
            assert not any(g.startswith("unmapped") for g in result.guardrail_events), (
                event.event_type
            )
            assert all(e.tenant_id == tenant for e in result.entities)
            assert all(e.source_event_id == event.source_event_id for e in result.entities)
            assert all(e.observed_at == event.source_ts for e in result.entities)


# -- GitHub ---------------------------------------------------------------------------


def test_github_member_snapshot_exact(base_settings: Settings) -> None:
    event = _fixture_event(base_settings, "acme", "github", "U_kgDOAcme1042@2026-08-25T09:00:00Z")
    result = map_event(event)
    ts = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)
    base: dict[str, Any] = {
        "tenant_id": TenantId("acme"),
        "source_connector": "github",
        "source_event_id": event.source_event_id,
        "observed_at": ts,
    }
    assert result.of(Person) == [
        Person(
            **base,
            entity_id="github:person:U_kgDOAcme1042",
            display_name="Jane Smith",
            emails=("jsmith@acme.example",),
            handles={"github": "jsmith"},
            account_kind=AccountKind.human,
            employment_status=EmploymentStatus.unknown,
        )
    ]
    assert result.of(AccessGrant) == [
        AccessGrant(
            **base,
            entity_id="github:grant:U_kgDOAcme1042:org:acme-example",
            person_entity_id="github:person:U_kgDOAcme1042",
            asset_entity_id="github:asset:org:acme-example",
            role="member",
            state=GrantState.active,
            state_effective_at=ts,
            attributes={"type": "User"},
        )
    ]
    [evidence] = result.of(Evidence)
    assert evidence.evidence_type is EvidenceType.org_membership
    assert evidence.subject_entity_id == "github:person:U_kgDOAcme1042"
    assert "jsmith (User) is member of GitHub organization acme-example" in evidence.text


def test_github_org_snapshot_yields_org_asset_and_vendor(base_settings: Settings) -> None:
    result = map_event(
        _fixture_event(base_settings, "acme", "github", "O_kgDOAcme0001@2026-08-25T09:00:00Z")
    )
    [asset] = result.of(Asset)
    assert asset.entity_id == "github:asset:O_kgDOAcme0001" and asset.asset_type == "org"
    assert asset.settings["two_factor_requirement_enabled"] is True
    [vendor] = result.of(Vendor)
    assert vendor.entity_id == "github:vendor:github" and vendor.name == "GitHub"


def test_github_repo_snapshot_yields_asset_and_collaborator_grants(base_settings: Settings) -> None:
    result = map_event(
        _fixture_event(base_settings, "acme", "github", "R_kgDOAcmeP001@2026-08-25T09:00:00Z")
    )
    [asset] = result.of(Asset)
    assert (
        asset.entity_id == "github:asset:R_kgDOAcmeP001" and asset.name == "acme-example/platform"
    )
    grants = {g.person_entity_id: g for g in result.of(AccessGrant)}
    assert grants["github:person:U_kgDOAcme1042"].role == "write"
    assert grants["github:person:U_kgDOAcmeBot01"].attributes == {"type": "Bot"}
    persons = {p.handles["github"]: p for p in result.of(Person)}
    assert persons["ci-runner"].account_kind is AccountKind.service
    assert persons["jsmith"].account_kind is AccountKind.human


def test_github_branch_protection_merges_into_repo_asset(base_settings: Settings) -> None:
    protected = map_event(
        _fixture_event(
            base_settings, "acme", "github", "R_kgDOAcmeP001:protection@2026-08-25T09:00:00Z"
        )
    )
    [asset] = protected.of(Asset)
    assert asset.entity_id == "github:asset:R_kgDOAcmeP001"
    assert asset.settings == {
        "branch_protection": {
            "protected": True,
            "branch": "main",
            "required_approving_review_count": 2,
            "enforce_admins": True,
        }
    }
    [evidence] = protected.of(Evidence)
    assert evidence.evidence_type is EvidenceType.repo_settings
    unprotected = map_event(
        _fixture_event(
            base_settings, "acme", "github", "R_kgDOAcmeI003:protection@2026-08-25T09:00:00Z"
        )
    )
    assert unprotected.of(Asset)[0].settings["branch_protection"] == {
        "protected": False,
        "branch": "main",
        "required_approving_review_count": 0,
        "enforce_admins": False,
    }


def test_github_security_policy_and_file_listing_are_evidence(base_settings: Settings) -> None:
    policy = map_event(
        _fixture_event(
            base_settings, "acme", "github", "R_kgDOAcmeP001:security@2026-06-02T14:20:00Z"
        )
    )
    [e] = policy.of(Evidence)
    assert (
        e.evidence_type is EvidenceType.policy_document
        and e.subject_entity_id == "github:asset:R_kgDOAcmeP001"
    )
    assert "5 business days" in e.text
    listing = map_event(
        _fixture_event(base_settings, "acme", "github", "R_kgDOAcmeP001:files@2026-08-25T09:00:00Z")
    )
    [f] = listing.of(Evidence)
    assert f.evidence_type is EvidenceType.repo_file_listing
    assert "### .env.example" in f.text and "ghp_" in f.text


def test_github_deliveries_add_and_revoke_grants(base_settings: Settings) -> None:
    added = map_event(_fixture_event(base_settings, "acme", "github", "acme-d-000341"))
    [grant] = added.of(AccessGrant)
    assert grant.entity_id == "github:grant:U_kgDOAcme1039:R_kgDOAcmeW002"
    assert grant.state is GrantState.active and grant.role == "write"
    removed = map_event(_fixture_event(base_settings, "acme", "github", "acme-d-000512"))
    [revoked] = removed.of(AccessGrant)
    assert revoked.entity_id == grant.entity_id and revoked.state is GrantState.revoked
    assert revoked.state_effective_at == datetime(2026, 7, 1, 15, 30, tzinfo=UTC)
    org_removed = map_event(_fixture_event(base_settings, "acme", "github", "acme-d-000513"))
    assert (
        org_removed.of(AccessGrant)[0].entity_id == "github:grant:U_kgDOAcme1039:org:acme-example"
    )


@pytest.mark.parametrize(
    ("login", "gh_type", "expected"),
    [
        ("jsmith", "User", AccountKind.human),
        ("ci-runner", "Bot", AccountKind.service),
        ("ci-runner", "User", AccountKind.service),
        ("deploy-bot", None, AccountKind.service),
        ("backup-svc", "User", AccountKind.service),
        ("ops-shared", "User", AccountKind.human),
    ],
)
def test_github_account_kind_rules(login: str, gh_type: str | None, expected: AccountKind) -> None:
    assert account_kind_of(login, gh_type) is expected


# -- HRIS ------------------------------------------------------------------------------


def test_hris_employee_snapshot_exact(base_settings: Settings) -> None:
    event = _fixture_event(base_settings, "acme", "hris", "E1042@2026-08-20T17:05:00Z")
    result = map_event(event)
    [person] = result.of(Person)
    assert person == Person(
        tenant_id=TenantId("acme"),
        entity_id="hris:person:E1042",
        source_connector="hris",
        source_event_id=event.source_event_id,
        observed_at=datetime(2026, 8, 20, 17, 5, tzinfo=UTC),
        display_name="Jane Smith",
        emails=("jsmith@acme.example",),
        handles={},
        employee_id="E1042",
        account_kind=AccountKind.human,
        employment_status=EmploymentStatus.terminated,
        status_effective_at=datetime(2026, 8, 20, 17, 0, tzinfo=UTC),
    )
    [roster] = result.of(Evidence)
    assert (
        roster.evidence_type is EvidenceType.hr_roster
        and roster.subject_entity_id == "hris:person:E1042"
    )
    assert result.of(Vendor)[0].entity_id == "hris:vendor:hris"


def test_hris_termination_event_sets_status_from_effective_time(base_settings: Settings) -> None:
    result = map_event(_fixture_event(base_settings, "acme", "hris", "acme-hr-evt-0388"))
    [person] = result.of(Person)
    assert person.entity_id == "hris:person:E1042"
    assert person.employment_status is EmploymentStatus.terminated
    assert person.status_effective_at == datetime(2026, 8, 20, 17, 0, tzinfo=UTC)


def test_hris_identity_yields_idp_grant_with_mfa(base_settings: Settings) -> None:
    result = map_event(
        _fixture_event(base_settings, "acme", "hris", "okta-00u1044@2026-08-25T08:00:00Z")
    )
    [grant] = result.of(AccessGrant)
    assert (
        grant.person_entity_id == "hris:person:E1044"
        and grant.asset_entity_id == "hris:asset:identity_provider"
    )
    assert grant.state is GrantState.active and grant.attributes["mfa_enabled"] is False
    deprovisioned = map_event(
        _fixture_event(base_settings, "acme", "hris", "okta-00u1042@2026-08-20T17:30:00Z")
    )
    assert deprovisioned.of(AccessGrant)[0].state is GrantState.revoked
    assert result.of(Asset)[0].entity_id == "hris:asset:identity_provider"
    assert result.of(Evidence)[0].evidence_type is EvidenceType.identity_mfa_status


def test_hris_shared_account_is_a_person_of_kind_shared_with_owner(base_settings: Settings) -> None:
    result = map_event(
        _fixture_event(base_settings, "acme", "hris", "acme-acct-0007@2026-08-25T08:00:00Z")
    )
    [person] = result.of(Person)
    assert person.entity_id == "hris:account:ops-shared"
    assert person.account_kind is AccountKind.shared and person.owner_employee_id is None
    owned = map_event(
        _fixture_event(base_settings, "acme", "hris", "acme-acct-0008@2026-08-25T08:00:00Z")
    )
    assert owned.of(Person)[0].owner_employee_id == "E1017"
    assert result.of(Evidence)[0].evidence_type is EvidenceType.account_registry


def test_hris_asset_and_document(base_settings: Settings) -> None:
    asset = map_event(
        _fixture_event(base_settings, "acme", "hris", "acme-sys-payroll@2026-08-03T10:00:00Z")
    ).of(Asset)[0]
    assert asset.name == "Payroll-SaaS" and asset.tags == ("critical",)
    conn = build_connector("hris", base_settings, "fixture")
    doc_event = next(
        e
        for e in conn.pull(TenantContext.for_id(base_settings, "acme"))
        if e.event_type == "hris.document" and e.payload["filename"] == "training-E1050.md"
    )
    [doc] = map_event(doc_event).of(Evidence)
    assert doc.evidence_type is EvidenceType.training_record
    assert doc.subject_entity_id == "hris:person:E1050"
    assert doc.issued_at == datetime(2026, 4, 2, tzinfo=UTC)
    assert "Sam Patil" in doc.text


# -- manual uploads and S13 ------------------------------------------------------------


def test_manual_pdf_upload_keeps_page_texts(base_settings: Settings) -> None:
    conn = build_connector("manual", base_settings, "fixture")
    event = next(
        e
        for e in conn.pull(TenantContext.for_id(base_settings, "globex"))
        if e.payload["filename"] == "access-review-2026-h1.pdf"
    )
    [evidence] = map_event(event).of(Evidence)
    assert evidence.evidence_type is EvidenceType.access_review_report
    assert evidence.page_texts is not None and len(evidence.page_texts) == 2
    assert "Payroll-GX" in evidence.page_texts[1]
    assert evidence.entity_id.startswith("manual:evidence:access-review-2026-h1.pdf:")


def _manual_event(tenant: str, payload: dict[str, Any]) -> SourceEvent:
    return SourceEvent(
        tenant_id=TenantId(tenant),
        connector="manual",
        source_event_id="upload:x:abc",
        source_ts=datetime(2026, 9, 1, tzinfo=UTC),
        event_type="manual.document",
        payload={
            "filename": "x.md",
            "sha256": "0" * 64,
            "size_bytes": 1,
            "evidence_type": "policy_document",
            "title": "x",
            "issued_at": "2026-01-01T00:00:00Z",
            "text": "body",
            **payload,
        },
        schema_version="manual.v1",
    )


def test_s13_tenant_claim_in_upload_is_ignored_and_logged() -> None:
    event = _manual_event(
        "acme", {"manifest": {"tenant": "globex"}, "front_matter": {"tenant": "globex"}}
    )
    result = map_manual_event(event)
    [evidence] = result.of(Evidence)
    assert evidence.tenant_id == "acme"
    assert set(result.guardrail_events) == {
        "upload_tenant_claim_ignored:manifest:globex",
        "upload_tenant_claim_ignored:front_matter:globex",
    }
    assert evidence.guardrail_events == result.guardrail_events


def test_no_text_layer_pdf_is_rejected_at_normalization() -> None:
    event = _manual_event(
        "acme",
        {
            "filename": "scan.pdf",
            "pdf": {"status": "no_text_layer", "page_count": 1, "pages": [""]},
            "text": "",
        },
    )
    result = map_manual_event(event)
    assert result.entities == ()
    assert result.guardrail_events == ("pdf_no_text_layer:scan.pdf",)


def test_unknown_evidence_type_is_rejected() -> None:
    result = map_manual_event(_manual_event("acme", {"evidence_type": "selfie"}))
    assert result.entities == () and result.guardrail_events == ("unknown_evidence_type:selfie",)
