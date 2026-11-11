"""Changing a tenant's settings (spec.md A-133).

An admin asks. A change to what decides a status (RULE_SETTINGS: the GitHub organization,
the admin limit, the critical asset tags, the revocation window) or to which accounts are
read (the connections) is a change of scope: it opens a scope_change item showing each
field before and after and the version it changes, and another member, an approver,
decides it. Approval records the new version in the decision's own transaction, and only
if the version it was asked against is still current, so two changes never silently
overwrite each other. Anything else (the name, the time zone, the budget, the schedules, the
label consent) an admin records directly, as a version of its own. Either way the next
evaluation reads the new settings, and a changed rule setting gives a new rule stamp, so
the tests that read it are evaluated again under the rule-change handling (A-128).
"""

from __future__ import annotations

from typing import Any

import psycopg
from ulid import ULID

from ccp import tenancy
from ccp.approvals import store as approvals
from ccp.auth import members
from ccp.auth.tokens import Principal
from ccp.config import TenantSettings
from ccp.evaluators.rule_versions import RULE_SETTINGS
from ccp.knowledge import graph as knowledge
from ccp.storage.scoped import ScopedDB
from ccp.tenants import versions

SCOPE_FIELDS = frozenset((*RULE_SETTINGS, "connections"))
FREE_FIELDS = frozenset(
    {
        "display_name",
        "time_zone",
        "daily_budget_usd",
        "max_concurrency",
        "schedules",
        "label_consent",
    }
)


class Superseded(ValueError):
    """The settings changed since the change was asked for: ask again against them."""


def _affected(fields: set[str], before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """The tests a change reaches: those whose rule names the setting among its parameters,
    and, for the GitHub organization or the connections, those reading that connector."""
    connectors: set[str] = set()
    if "github_org" in fields:
        connectors.add("github")
    if "connections" in fields:
        for side in (before, after):
            connectors.update(str(c.get("connector")) for c in side.get("connections") or ())
    out = {
        spec.test_id
        for spec in knowledge.load().tests.values()
        if {str(v) for v in spec.parameters.values()} & fields
        or set(spec.connector_scope) & connectors
    }
    return sorted(out)


def change(db: ScopedDB, who: Principal, changes: dict[str, Any]) -> tuple[str | None, str | None]:
    """(the version recorded at once, the scope_change item opened): one of the two."""
    members.require(db, who, "admin")
    unknown = set(changes) - SCOPE_FIELDS - FREE_FIELDS
    if unknown:
        raise ValueError(f"no such setting: {sorted(unknown)}")
    current, from_version = versions.effective(db)
    before = current.model_dump(mode="json")
    proposed = TenantSettings.model_validate({**before, **changes})
    after = proposed.model_dump(mode="json")
    changed = {k for k in changes if before.get(k) != after.get(k)}
    if not changed:
        raise ValueError("nothing changes")
    if not changed & SCOPE_FIELDS:
        version = versions.record(
            db,
            proposed,
            reason="settings: " + ", ".join(sorted(changed)),
            recorded_by=who.principal_id,
        )
        return version, None
    payload = {
        "tenant_id": db.tenant.tenant_id,
        "from_version": from_version,
        "changes": {k: {"from": before.get(k), "to": after.get(k)} for k in sorted(changed)},
        "settings": after,
        "affects": _affected(changed & SCOPE_FIELDS, before, after),
    }
    item = approvals.open_item(
        db,
        "scope_change",
        f"{db.tenant.tenant_id}:{ULID()}",
        payload,
        opened_by=who.label,
        requested_by=who.principal_id,
    )
    return None, item


def apply_in(
    cur: psycopg.Cursor[Any], db: ScopedDB, item: approvals.Approval, approver: Principal
) -> str:
    """Record the approved settings as the new version, inside the decision's transaction;
    refuse if the version it was asked against is no longer current."""
    asked = item.payload
    _, now_version = versions.effective(db)
    if now_version != asked["from_version"]:
        raise Superseded(
            f"the settings changed since this was asked (version {asked['from_version']} is"
            f" no longer current); ask again against {now_version}"
        )
    return versions.record_in(
        cur,
        db.tenant.tenant_id,
        TenantSettings.model_validate(asked["settings"]),
        reason="scope change: " + ", ".join(sorted(asked["changes"])),
        recorded_by=approver.principal_id,
        approval_id=item.approval_id,
    )


def forget(db: ScopedDB) -> None:
    tenancy.forget(db.tenant.tenant_id)
