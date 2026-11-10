"""GitHub SourceEvent -> canonical entities. Pure: no I/O, no lookups, no shared state.

    org.snapshot                    -> Asset(org), Vendor(GitHub)
    org.member.snapshot             -> Person, AccessGrant(person -> org), Evidence(org_membership)
    repo.snapshot                   -> Asset(repo); per collaborator Person + AccessGrant(-> repo)
    repo.branch_protection.snapshot -> Asset(repo, settings.branch_protection), Evidence(settings)
    repo.security_policy.snapshot   -> Evidence(policy_document)
    repo.file_listing.snapshot      -> Evidence(repo_file_listing)
    org.member_added/removed        -> Person, AccessGrant(person -> org, active/revoked)
    repo.collaborator_added/removed -> Person, AccessGrant(person -> repo, active/revoked)

Account kind: the API's ``type`` (Bot -> service) plus a small login-pattern list; ``shared``
is never inferred here (spec.md Layer 3).
"""

from __future__ import annotations

from typing import Any

from ccp.connectors.base import SourceEvent
from ccp.normalize.entities import (
    AccessGrant,
    Asset,
    Entity,
    Evidence,
    MappingResult,
    Person,
    Vendor,
)
from ccp.types import AccountKind, EmploymentStatus, EvidenceType, GrantState

_SERVICE_PATTERNS = ("-bot", "-svc", "ci-", "deploy-")
# A file listing says what it did not read, so GH-5 cannot pass over it (A-96).
NOT_INLINED = "(content not inlined)"
UNREAD_EVENT_PREFIXES = ("files_not_read:", "entries_not_listed:")


def account_kind_of(login: str, gh_type: str | None) -> AccountKind:
    if gh_type == "Bot":
        return AccountKind.service
    lowered = login.lower()
    if lowered.endswith(_SERVICE_PATTERNS[:2]) or lowered.startswith(_SERVICE_PATTERNS[2:]):
        return AccountKind.service
    return AccountKind.human


def person_id(node_id: str) -> str:
    return f"github:person:{node_id}"


def asset_id(node_id: str) -> str:
    return f"github:asset:{node_id}"


def grant_id(person_node: str, asset_node: str) -> str:
    return f"github:grant:{person_node}:{asset_node}"


def _person(
    event: SourceEvent,
    login: str,
    node_id: str,
    gh_type: str | None,
    name: str | None = None,
    emails: list[str] | None = None,
) -> Person:
    return Person(
        tenant_id=event.tenant_id,
        entity_id=person_id(node_id),
        source_connector="github",
        source_event_id=event.source_event_id,
        observed_at=event.source_ts,
        display_name=name or login,
        emails=tuple(emails or ()),
        handles={"github": login},
        account_kind=account_kind_of(login, gh_type),
        employment_status=EmploymentStatus.unknown,
    )


def _grant(
    event: SourceEvent,
    person_node: str,
    asset_node: str,
    role: str,
    state: GrantState,
    attributes: dict[str, Any] | None = None,
) -> AccessGrant:
    return AccessGrant(
        tenant_id=event.tenant_id,
        entity_id=grant_id(person_node, asset_node),
        source_connector="github",
        source_event_id=event.source_event_id,
        observed_at=event.source_ts,
        person_entity_id=person_id(person_node),
        asset_entity_id=asset_id(asset_node),
        role=role,
        state=state,
        state_effective_at=event.source_ts,
        attributes=attributes or {},
    )


def _evidence(
    event: SourceEvent,
    entity_id: str,
    evidence_type: EvidenceType,
    title: str,
    text: str,
    subject: str | None,
    raw_ref: str,
) -> Evidence:
    return Evidence(
        tenant_id=event.tenant_id,
        entity_id=entity_id,
        source_connector="github",
        source_event_id=event.source_event_id,
        observed_at=event.source_ts,
        evidence_type=evidence_type,
        title=title,
        text=text,
        subject_entity_id=subject,
        issued_at=event.source_ts,
        raw_ref=raw_ref,
    )


def map_github_event(event: SourceEvent) -> MappingResult:
    p = event.payload
    kind = event.event_type
    out: list[Entity] = []

    if kind == "org.snapshot":
        out.append(
            Asset(
                tenant_id=event.tenant_id,
                entity_id=asset_id(_org_node_from_login(p["login"])),  # same id grants point at
                source_connector="github",
                source_event_id=event.source_event_id,
                observed_at=event.source_ts,
                name=p["login"],
                asset_type="org",
                tags=(),
                settings={
                    "two_factor_requirement_enabled": p.get("two_factor_requirement_enabled"),
                    "default_repository_permission": p.get("default_repository_permission"),
                },
            )
        )
        out.append(
            Vendor(
                tenant_id=event.tenant_id,
                entity_id="github:vendor:github",
                source_connector="github",
                source_event_id=event.source_event_id,
                observed_at=event.source_ts,
                name="GitHub",
                vendor_type="source_control",
                data_categories=("source_code", "identities"),
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "org.member.snapshot":
        org_node = _org_node(p)
        out.append(
            _person(
                event,
                p["login"],
                p["node_id"],
                p.get("type"),
                p.get("name"),
                p.get("verified_domain_emails"),
            )
        )
        out.append(
            _grant(
                event, p["node_id"], org_node, p["role"], GrantState.active, {"type": p.get("type")}
            )
        )
        emails = ", ".join(p.get("verified_domain_emails") or []) or "none"
        out.append(
            _evidence(
                event,
                f"github:evidence:{p['node_id']}:membership",
                EvidenceType.org_membership,
                f"GitHub organization membership: {p['login']}",
                f"{p['login']} ({p.get('type', 'User')}) is {p['role']} of GitHub organization "
                f"{p['org']}. Verified domain emails: {emails}. Snapshot {p['updated_at']}.",
                person_id(p["node_id"]),
                f"github:orgs/{p['org']}/members/{p['login']}",
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "repo.snapshot":
        out.append(
            Asset(
                tenant_id=event.tenant_id,
                entity_id=asset_id(p["node_id"]),
                source_connector="github",
                source_event_id=event.source_event_id,
                observed_at=event.source_ts,
                name=p["full_name"],
                asset_type="repo",
                tags=(),
                settings={"private": p.get("private"), "default_branch": p.get("default_branch")},
            )
        )
        for c in p.get("collaborators", []):
            out.append(_person(event, c["login"], c["node_id"], c.get("type")))
            out.append(
                _grant(
                    event,
                    c["node_id"],
                    p["node_id"],
                    c["permission"],
                    GrantState.active,
                    {"type": c.get("type")},
                )
            )
        return MappingResult(entities=tuple(out))

    if kind == "repo.branch_protection.snapshot":
        reviews = p.get("required_pull_request_reviews") or {}
        enforce = p.get("enforce_admins") or {}
        protection = {
            "protected": bool(p.get("protected")),
            "branch": p.get("branch"),
            "required_approving_review_count": reviews.get("required_approving_review_count", 0)
            if p.get("protected")
            else 0,
            "enforce_admins": bool(enforce.get("enabled")) if p.get("protected") else False,
        }
        out.append(
            Asset(
                tenant_id=event.tenant_id,
                entity_id=asset_id(p["repo_node_id"]),
                source_connector="github",
                source_event_id=event.source_event_id,
                observed_at=event.source_ts,
                name=f"{p['org']}/{p['repo']}",
                asset_type="repo",
                tags=(),
                settings={"branch_protection": protection},
            )
        )
        text = (
            f"Branch protection for {p['org']}/{p['repo']} on {p.get('branch')}: "
            f"protected={protection['protected']}, required approving reviews="
            f"{protection['required_approving_review_count']}, "
            f"enforce_admins={protection['enforce_admins']}."
        )
        out.append(
            _evidence(
                event,
                f"github:evidence:{p['repo_node_id']}:protection",
                EvidenceType.repo_settings,
                f"Branch protection: {p['org']}/{p['repo']}",
                text,
                asset_id(p["repo_node_id"]),
                f"github:repos/{p['org']}/{p['repo']}/branches/{p.get('branch')}/protection",
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "repo.security_policy.snapshot":
        out.append(
            _evidence(
                event,
                f"github:evidence:{p['repo_node_id']}:security",
                EvidenceType.policy_document,
                f"SECURITY.md: {p['org']}/{p['repo']}",
                p["content"],
                asset_id(p["repo_node_id"]),
                f"github:repos/{p['org']}/{p['repo']}/{p.get('path', 'SECURITY.md')}",
            )
        )
        return MappingResult(entities=tuple(out))

    if kind == "repo.file_listing.snapshot":
        parts = []
        unread = 0
        for f in p.get("files", []):
            body = f.get("content")
            unread += body is None
            parts.append(f"### {f['path']}\n{body if body is not None else NOT_INLINED}")
        not_listed = [str(e.get("path")) for e in p.get("not_listed", [])]
        if not_listed:
            parts.append("### Not listed\n" + "\n".join(f"- {path}" for path in not_listed))
        events = []
        if unread:
            events.append(f"files_not_read:{unread}")
        if not_listed:
            events.append(f"entries_not_listed:{len(not_listed)}")
        listing = _evidence(
            event,
            f"github:evidence:{p['repo_node_id']}:files",
            EvidenceType.repo_file_listing,
            f"Tracked files: {p['org']}/{p['repo']}",
            "\n\n".join(parts),
            asset_id(p["repo_node_id"]),
            f"github:repos/{p['org']}/{p['repo']}/contents",
        )
        out.append(listing.model_copy(update={"guardrail_events": tuple(events)}))
        return MappingResult(entities=tuple(out))

    if kind in ("org.member_added", "org.member_removed"):
        inner = p["payload"]
        user = inner["membership"]["user"]
        org_login = inner["organization"]["login"]
        org_node = _org_node_from_login(org_login)
        state = GrantState.active if kind == "org.member_added" else GrantState.revoked
        out.append(_person(event, user["login"], user["node_id"], user.get("type")))
        out.append(
            _grant(
                event,
                user["node_id"],
                org_node,
                inner["membership"].get("role", "member"),
                state,
                {"type": user.get("type"), "delivery": p["id"]},
            )
        )
        return MappingResult(entities=tuple(out))

    if kind in ("repo.collaborator_added", "repo.collaborator_removed"):
        inner = p["payload"]
        user = inner["member"]
        repo = inner["repository"]
        state = GrantState.active if kind == "repo.collaborator_added" else GrantState.revoked
        role = (
            (inner.get("changes") or {}).get("permission", {}).get("to", "write")
            if state is GrantState.active
            else "revoked"
        )
        out.append(_person(event, user["login"], user["node_id"], user.get("type")))
        out.append(
            _grant(
                event,
                user["node_id"],
                repo["node_id"],
                role,
                state,
                {"type": user.get("type"), "delivery": p["id"]},
            )
        )
        return MappingResult(entities=tuple(out))

    return MappingResult(guardrail_events=(f"unmapped_event_type:{kind}",))


def _org_node(p: dict[str, Any]) -> str:
    return _org_node_from_login(str(p["org"]))


def _org_node_from_login(login: str) -> str:
    """Org node ids are stable per login in this build; deliveries carry the login only."""
    return f"org:{login}"
