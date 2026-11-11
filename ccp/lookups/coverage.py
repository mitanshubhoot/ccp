"""The coverage map: every inventoried resource, every intent of its class, and what
decides it (spec.md A-166). Coverage is measured, never assumed:

    pass / fail     a deterministic test covers the intent and its latest verdict judged
                    the resource (named among the failing, or judged and not named)
    pending         a test covers it but has no verdict that judged the resource yet
    gap             no test covers the intent: the class's playbook reads it (A-167),
                    gap_fail / gap_needs_review once it has
    not_assessed    the resource's type has no approved class, or the account's
                    inventory is not whole
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any

from ccp.evaluators import verdict as verdicts
from ccp.knowledge import service_classes
from ccp.lookups import inventory
from ccp.lookups.store import failing_of
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus


@dataclass(frozen=True)
class Row:
    account: str
    resource_type: str
    resource_id: str
    name: str
    service_class: str | None
    intent: str | None
    status: str
    test_id: str | None = None
    verdict_id: str | None = None
    detail: str = ""


def build(db: ScopedDB) -> list[Row]:
    classes = service_classes.load()
    heads: dict[str, Any] = {}
    for head in bitemporal.heads(db):
        heads.setdefault(head.test_id, head)
    judged: dict[str, tuple[Any, set[str]]] = {}
    for test_id, head in heads.items():
        found = verdicts.load(db, head.verdict_id) if head.verdict_id else None
        if found is not None:
            judged[test_id] = (found, {f["name"] for f in failing_of(found)})
    playbooks: dict[tuple[str, str, str], str] = {
        (str(r[0]), str(r[1]), str(r[2])): str(r[3])
        for r in db.fetch_all(
            "SELECT DISTINCT ON (resource_type, resource_id, intent) resource_type, resource_id,"
            " intent, status FROM playbook_results"
            " ORDER BY resource_type, resource_id, intent, created_at DESC"
        )
    }
    rows: list[Row] = []
    for snapshot in inventory.latest(db):
        account = str(snapshot["account"])
        for r in snapshot["resources"]:
            rtype, rid, name = r["resource_type"], r["resource_id"], r["name"]
            cls = classes.class_of(rtype)
            if cls is None:
                rows.append(
                    Row(
                        account,
                        rtype,
                        rid,
                        name,
                        None,
                        None,
                        "not_assessed",
                        detail="unclassified type",
                    )
                )
                continue
            for intent in classes.intents[cls]:
                if not snapshot["complete"]:
                    rows.append(
                        Row(
                            account,
                            rtype,
                            rid,
                            name,
                            cls,
                            intent,
                            "not_assessed",
                            detail=snapshot["detail"],
                        )
                    )
                    continue
                covering = classes.covering_test(rtype, intent)
                if covering is None:
                    read = playbooks.get((rtype, rid, intent))
                    rows.append(
                        Row(
                            account,
                            rtype,
                            rid,
                            name,
                            cls,
                            intent,
                            f"gap_{read}" if read in ("fail", "needs_review") else "gap",
                        )
                    )
                    continue
                seen = judged.get(covering)
                if seen is None or seen[0].status == VerdictStatus.needs_review:
                    rows.append(Row(account, rtype, rid, name, cls, intent, "pending", covering))
                    continue
                verdict, failing = seen
                failed = verdict.status == VerdictStatus.failed and (
                    name in failing or account in failing
                )
                rows.append(
                    Row(
                        account,
                        rtype,
                        rid,
                        name,
                        cls,
                        intent,
                        "fail" if failed else "pass",
                        covering,
                        verdict.verdict_id,
                    )
                )
    return rows


def summary(rows: list[Row]) -> dict[str, Any]:
    """The numbers LT11 is judged by: resources without a status (there must be none),
    gaps, and the share of intents a deterministic test decides."""
    by_status = Counter(r.status for r in rows)
    intents = [r for r in rows if r.intent is not None]
    decided = [r for r in intents if r.test_id is not None]
    return {
        "resources": len({(r.account, r.resource_type, r.resource_id) for r in rows}),
        "by_status": dict(by_status),
        "deterministic_share": round(len(decided) / len(intents), 4) if intents else None,
        "without_status": sum(1 for r in rows if not r.status),
    }


def as_dicts(rows: list[Row]) -> list[dict[str, Any]]:
    return [asdict(r) for r in rows]
