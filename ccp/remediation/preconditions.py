"""Preconditions (spec.md A-131): the facts a proposal was made under, computed by code.

A proposal records them, and their hash, when it is created; its approval payload carries
the hash, and so does the grant its decision mints. The executor computes them again just
before it acts and refuses on any difference, so an approval given for one situation never
acts in another: the failure resolved meanwhile, the access changed, or the change it would
undo was already undone.

remove_repo_collaborator, remove_org_member   the subject's newest verdict status, and the
                                               access the login holds on the target now
notify_owner, request_evidence                 the subject's newest verdict status
a rollback (restore_*)                         the reversed proposal's status and the
                                               permission recorded before it acted
"""

from __future__ import annotations

from typing import Any, Protocol

from ccp.approvals.store import payload_hash
from ccp.evaluators import verdict as verdicts
from ccp.normalize import repository
from ccp.storage.scoped import ScopedDB


class Reversed(Protocol):
    @property
    def proposal_id(self) -> str: ...

    @property
    def status(self) -> str: ...

    @property
    def pre_state(self) -> dict[str, Any] | None: ...


def _access(db: ScopedDB, login: str, *, repo: str = "", org: str = "") -> dict[str, str] | None:
    """The login's current grant on one repository (by name) or one organization."""
    people = {
        p.entity_id for p in repository.current_persons(db) if p.handles.get("github") == login
    }
    if not people:
        return None
    if org:
        assets = {f"github:asset:org:{org}"}
    else:
        assets = {a.entity_id for a in repository.current_assets(db) if a.name == repo}
    found = sorted(
        (g.role, g.state.value)
        for g in repository.current_grants(db)
        if g.person_entity_id in people and g.asset_entity_id in assets
    )
    if not found:
        return None
    role, state = found[-1]
    return {"role": role, "state": state}


def current(
    db: ScopedDB,
    *,
    verdict_id: str,
    action: str,
    target: dict[str, str],
    reverses: Reversed | None = None,
) -> dict[str, Any]:
    if reverses is not None:
        return {
            "reverses": reverses.proposal_id,
            "status": reverses.status,
            "restore": (reverses.pre_state or {}).get("permission"),
        }
    verdict = verdicts.load(db, verdict_id)
    subject_key = verdict.subject_key if verdict is not None else None
    newest = verdicts.latest_for_subject(db, subject_key) if subject_key else None
    facts: dict[str, Any] = {
        "subject_key": subject_key,
        "status": newest.status.value if newest is not None else None,
    }
    if action == "remove_repo_collaborator":
        facts["access"] = _access(db, target.get("login", ""), repo=target.get("repo", ""))
    elif action == "remove_org_member":
        facts["access"] = _access(db, target.get("login", ""), org=target.get("org", ""))
    return facts


def digest(facts: dict[str, Any]) -> str:
    return payload_hash(facts)


def differences(then: dict[str, Any], now: dict[str, Any]) -> list[str]:
    """What changed, in words, for the refusal."""
    keys = sorted(set(then) | set(now))
    return [f"{k}: {then.get(k)!r} -> {now.get(k)!r}" for k in keys if then.get(k) != now.get(k)]
