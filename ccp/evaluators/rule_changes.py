"""Rule changes: amendments by default, corrections by decision (spec.md A-128).

A rule's code or parameters changing took effect silently: the next evaluation of each
subject ran the new rule, and because a rule computes its own valid time (a termination plus
the SLA, the evidence's observation), its verdict quietly rewrote the subject's past. Now:

    amendment    the default. A verdict decided under a new rule stamp that replaces a head
                 decided under another is recorded with reason `rule_change`, valid no earlier
                 than the new stamp's first verdict for the tenant, so the past stands. The
                 first such row appends a notice to `rule_changes`.
    correction   a decision: the old rule was wrong. `correct` re-decides each subject's last
                 verdict under an old stamp with the new rule and the tenant's current
                 parameters, from the very inputs that verdict read (A-125). The result
                 supersedes the head at the old verdict's valid time, with reason
                 `correction`. A correction turning a FAIL into a PASS needs a named
                 sign-off, and it appends a notice with every subject's before and after.

`preview` shows the before and after first, and works whether or not the new rule has run.
CCP decides the kind and records the notice; no customer acknowledgement is asked (U11).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg
from ulid import ULID

from ccp.config import Settings
from ccp.evaluators import rederive
from ccp.evaluators import verdict as verdicts
from ccp.evaluators.rule_versions import rule_stamp
from ccp.evaluators.runner import cached_graph, commit_verdict
from ccp.evaluators.verdict import Verdict
from ccp.storage.scoped import ScopedDB


@dataclass(frozen=True)
class Flip:
    subject_key: str
    before: str
    after: str


@dataclass(frozen=True)
class Preview:
    test_id: str
    from_stamps: tuple[str, ...]
    to_stamp: str
    subjects: tuple[Flip, ...]

    @property
    def fail_to_pass(self) -> list[Flip]:
        return [f for f in self.subjects if f.before == "fail" and f.after == "pass"]

    @property
    def changed(self) -> list[Flip]:
        return [f for f in self.subjects if f.before != f.after]


@dataclass(frozen=True)
class Corrected:
    change_id: str
    preview: Preview
    verdict_ids: tuple[str, ...]
    notice: str


class SignOffRequired(PermissionError):
    """A correction that turns a FAIL into a PASS waits for a named person's sign-off."""


def current_stamp(db: ScopedDB, test_id: str) -> str:
    return rule_stamp(cached_graph().tests[test_id], db.tenant.settings)


def _old_verdicts(db: ScopedDB, test_id: str, stamp: str) -> list[Verdict]:
    """Per subject, the newest verdict the state recorded under a stamp other than `stamp`,
    unless a correction under `stamp` has superseded it already."""
    rows = db.fetch_all(
        "SELECT DISTINCT ON (c.subject_key) c.verdict_id FROM compliance_state c"
        " JOIN verdicts v ON v.verdict_id = c.verdict_id"
        " WHERE c.test_id = %s AND v.tier = 1 AND v.rule_version IS DISTINCT FROM %s"
        " ORDER BY c.subject_key, c.recorded_seq DESC",
        (test_id, stamp),
    )
    corrected = {
        str(r[0])
        for r in db.fetch_all(
            "SELECT subject_key FROM verdicts WHERE test_id = %s AND rule_version = %s"
            " AND trigger_reason = 'correction'",
            (test_id, stamp),
        )
    }
    out: list[Verdict] = []
    for (verdict_id,) in rows:
        verdict = verdicts.load(db, str(verdict_id))
        if verdict is not None and verdict.manifest and verdict.subject_key not in corrected:
            out.append(verdict)
    return out


def _redecided(db: ScopedDB, settings: Settings, verdict: Verdict) -> Verdict:
    result = rederive.rederive(db, settings, verdict, parameters="current")
    assert result.derived is not None
    return result.derived


def preview(db: ScopedDB, settings: Settings, test_id: str) -> Preview:
    """Each subject's last verdict under an old rule, and what the current rule decides from
    the same inputs."""
    stamp = current_stamp(db, test_id)
    old = _old_verdicts(db, test_id, stamp)
    flips = tuple(
        Flip(v.subject_key, v.status.value, _redecided(db, settings, v).status.value) for v in old
    )
    stamps = tuple(sorted({v.rule_version or "" for v in old}))
    return Preview(test_id, stamps, stamp, flips)


def _notice(kind: str, test_id: str, before: tuple[str, ...], after: str, tail: str) -> str:
    return f"{kind} of {test_id}: rule {', '.join(before) or 'none'} -> {after}; {tail}"


def _record(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    test_id: str,
    before: tuple[str, ...],
    after: str,
    kind: str,
    effective_from: datetime | None,
    decided_by: str,
    signed_off_by: str | None,
    subjects: list[dict[str, str]],
    notice: str,
) -> str:
    change_id = str(ULID())
    cur.execute(
        "INSERT INTO rule_changes (tenant_id, change_id, test_id, from_stamps, to_stamp, kind,"
        " effective_from, decided_at, decided_by, signed_off_by, subjects, notice)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (tenant_id, test_id, to_stamp, kind) DO NOTHING",
        (
            tenant_id,
            change_id,
            test_id,
            json.dumps(list(before)),
            after,
            kind,
            effective_from,
            datetime.now(UTC),
            decided_by,
            signed_off_by,
            json.dumps(subjects),
            notice,
        ),
    )
    return change_id


def note_amendment(
    cur: psycopg.Cursor[Any],
    tenant_id: str,
    test_id: str,
    before: str,
    after: str,
    effective_from: datetime,
) -> None:
    """The notice of an amendment, appended once per test and new stamp by the state writer
    when it first records a subject re-decided under the new rule."""
    tail = (
        f"effective {effective_from.isoformat()}; each subject's re-decision is a state row"
        " with reason rule_change, and the past stands"
    )
    _record(
        cur,
        tenant_id,
        test_id,
        (before,),
        after,
        "amendment",
        effective_from,
        "ccp",
        None,
        [],
        _notice("amendment", test_id, (before,), after, tail),
    )


def correct(
    db: ScopedDB,
    settings: Settings,
    test_id: str,
    decided_by: str,
    signed_off_by: str | None = None,
    now: datetime | None = None,
) -> Corrected:
    """Re-decide the test's past under its current rule, and record the notice."""
    if not decided_by.strip():
        raise ValueError("a correction names who decided it")
    shown = preview(db, settings, test_id)
    if not shown.subjects:
        raise LookupError(f"nothing of {test_id} was decided under another rule")
    if shown.fail_to_pass and not (signed_off_by or "").strip():
        flips = ", ".join(f.subject_key for f in shown.fail_to_pass)
        raise SignOffRequired(f"a correction turning FAIL into PASS needs a sign-off: {flips}")
    moment = now or datetime.now(UTC)
    written: list[str] = []
    for old in _old_verdicts(db, test_id, shown.to_stamp):
        new = _redecided(db, settings, old).model_copy(
            update={"trigger_reason": "correction", "created_at": moment}
        )
        if commit_verdict(db, new):
            written.append(new.verdict_id)
    tail = f"{len(shown.subjects)} subject(s), {len(shown.changed)} change status"
    tail += "".join(f"; {f.subject_key}: {f.before} -> {f.after}" for f in shown.changed)
    notice = _notice("correction", test_id, shown.from_stamps, shown.to_stamp, tail)
    with db.transaction() as cur:
        change_id = _record(
            cur,
            db.tenant.tenant_id,
            test_id,
            shown.from_stamps,
            shown.to_stamp,
            "correction",
            None,
            decided_by,
            signed_off_by,
            [f.__dict__ for f in shown.subjects],
            notice,
        )
    return Corrected(change_id, shown, tuple(written), notice)
