"""As-of audit packages (spec.md A-168): the record of one framework at one moment, in a
form an auditor can check without trusting CCP.

A package holds, for every subject of every test the framework's controls rest on, the
state that was true at `valid_at` by what CCP had recorded at `known_at` (the bitemporal
read, A-107):

    results.json      each result's status and verdict, its method (tier, rule version or
                      model, prompt and release bundle, catalogue version), the raw pointers
                      of the evidence it read (stored digest and version), and its proof:
                      the verdict row's chained text and sequence number
    chain.json        the tenant's audit chain from the first proven row to a checkpoint
                      signed for this package (A-137)
    corrections.json  for a delta: what changed from the package it corrects, and why
    manifest.json     every file's digest, the signed checkpoint and the public key;
    manifest.sig      signed with the same key

The folder is sealed write-once (a compliance-mode bucket in production) and registered in
`audit_packages`; `ccp audit package verify`, or ccp/audit/package_verify.py on its own,
checks it offline.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer
from ulid import ULID

from ccp.audit import checkpoints
from ccp.audit.package_verify import MANIFEST, SIGNATURE, canonical, sha256_file
from ccp.audit.signing import Signer, b64, signer_for
from ccp.config import Settings
from ccp.evaluators import verdict as verdicts
from ccp.knowledge import catalogue
from ccp.knowledge import graph as knowledge
from ccp.storage import bitemporal
from ccp.storage.scoped import ScopedDB

_ENTITY_TABLES = ("persons", "access_grants", "assets", "evidence")


def _raw_pointers(
    db: ScopedDB, entity_ids: tuple[str, ...], known_at: datetime
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        for table in _ENTITY_TABLES:
            row = db.fetch_one(
                f"SELECT source_connector, source_event_id FROM {table}"  # noqa: S608
                " WHERE entity_id = %s AND observed_at <= %s"
                " ORDER BY observed_at DESC, source_event_id DESC LIMIT 1",
                (entity_id, known_at),
            )
            if row is None:
                continue
            raw = db.fetch_one(
                "SELECT raw_path, stored_sha256, version_id, store FROM raw_events"
                " WHERE connector = %s AND source_event_id = %s",
                (row[0], row[1]),
            )
            out.append(
                {
                    "entity_id": entity_id,
                    "source_event_id": row[1],
                    "raw_ref": raw[0] if raw else None,
                    "stored_sha256": raw[1] if raw else None,
                    "version_id": raw[2] if raw else None,
                    "store": raw[3] if raw else None,
                }
            )
            break
    return out


def _proof(db: ScopedDB, verdict_id: str) -> dict[str, Any] | None:
    """The verdict row's chained text, exactly as the chain hashed it, and its seq."""
    row = db.fetch_one(
        "SELECT c.seq, (SELECT jsonb_object_agg(e.key, e.value) FROM jsonb_each(to_jsonb(v)) e"
        " WHERE e.key = ANY (c.row_columns))::text"
        " FROM audit_chain c JOIN verdicts v ON (c.row_key ->> 'verdict_id') = v.verdict_id"
        " WHERE c.table_name = 'verdicts' AND v.verdict_id = %s",
        (verdict_id,),
    )
    return {"seq": int(row[0]), "row_text": str(row[1])} if row else None


def _subjects(db: ScopedDB, tests: list[str]) -> list[str]:
    rows = db.fetch_all(
        "SELECT DISTINCT subject_key FROM compliance_state WHERE test_id = ANY(%s) ORDER BY 1",
        (tests,),
    )
    return [str(r[0]) for r in rows]


def results_as_of(
    db: ScopedDB, framework_id: str, valid_at: datetime, known_at: datetime
) -> list[dict[str, Any]]:
    graph = knowledge.load()
    framework = graph.frameworks[framework_id]
    tests_by_requirement = {
        r.requirement_id: [
            t.test_id for t in graph.tests_for_requirement(framework_id, r.requirement_id)
        ]
        for r in framework.requirements
    }
    tests = sorted({t for ts in tests_by_requirement.values() for t in ts})
    out: list[dict[str, Any]] = []
    for subject_key in _subjects(db, tests):
        state = bitemporal.state_as_of(db, subject_key, valid_at, known_at)
        if state is None:
            continue
        found = verdicts.load(db, state.verdict_id)
        if found is None:
            continue
        out.append(
            {
                "requirements": sorted(
                    r for r, ts in tests_by_requirement.items() if state.test_id in ts
                ),
                "test_id": state.test_id,
                "subject_key": subject_key,
                "status": state.status.value,
                "verdict_id": state.verdict_id,
                "valid_from": state.valid_from.isoformat(),
                "method": {
                    "tier": found.tier,
                    "rule_version": found.rule_version,
                    "model": found.model,
                    "prompt_version": found.prompt_version,
                    "bundle_id": found.bundle_id,
                    "catalogue_version": state.catalogue_version,
                },
                "evidence": _raw_pointers(db, found.evidence_refs, known_at),
                "proof": _proof(db, state.verdict_id),
            }
        )
    return out


def _chain(db: ScopedDB, start: int, end: int) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        "SELECT seq, table_name, row_sha256, prev_sha256, entry_sha256 FROM audit_chain"
        " WHERE seq >= %s AND seq <= %s ORDER BY seq",
        (start, end),
    )
    return [
        {
            "seq": int(r[0]),
            "table_name": r[1],
            "row_sha256": r[2],
            "prev_sha256": r[3],
            "entry_sha256": r[4],
        }
        for r in rows
    ]


def build(
    db: ScopedDB,
    settings: Settings,
    framework_id: str,
    valid_at: datetime,
    known_at: datetime | None = None,
    *,
    by: str,
    corrects: str | None = None,
    signer: Signer | None = None,
    store: checkpoints.CheckpointStore | None = None,
    out_dir: Path | None = None,
) -> tuple[str, Path]:
    """Assemble, sign, seal and register a package; a correction carries only what
    changed since the package it corrects."""
    known = known_at or datetime.now(UTC)
    if known < valid_at:
        raise ValueError("a package cannot know less than the moment it is about")
    active = signer or signer_for(settings)
    results = results_as_of(db, framework_id, valid_at, known)
    corrections: list[dict[str, Any]] = []
    if corrects is not None:
        before_row = db.fetch_one(
            "SELECT results, framework_id FROM audit_packages WHERE package_id = %s", (corrects,)
        )
        if before_row is None or before_row[1] != framework_id:
            raise ValueError(f"no package {corrects!r} for {framework_id}")
        before = before_row[0] if isinstance(before_row[0], dict) else json.loads(before_row[0])
        changed = []
        for r in results:
            prior = before.get(r["subject_key"])
            if prior is None or prior["verdict_id"] != r["verdict_id"]:
                changed.append(r)
                corrections.append(
                    {
                        "subject_key": r["subject_key"],
                        "before": prior,
                        "after": {"verdict_id": r["verdict_id"], "status": r["status"]},
                    }
                )
        index = {
            r["subject_key"]: {"verdict_id": r["verdict_id"], "status": r["status"]}
            for r in results
        }
        results = changed
    else:
        index = {
            r["subject_key"]: {"verdict_id": r["verdict_id"], "status": r["status"]}
            for r in results
        }
    point = checkpoints.checkpoint(db, settings, signer=active, store=store) or checkpoints.latest(
        db
    )
    if point is None:
        raise ValueError("the tenant's chain is empty: nothing to prove a package against")
    proven = [r["proof"]["seq"] for r in results if r.get("proof")]
    chain = _chain(db, min(proven), point.seq) if proven else []
    package_id = str(ULID())
    folder = (
        out_dir or settings.ccp_data_dir / "exports" / db.tenant.tenant_id / "packages"
    ) / package_id
    folder.mkdir(parents=True)
    (folder / "results.json").write_text(
        json.dumps(results, indent=2, default=str), encoding="utf-8"
    )
    (folder / "chain.json").write_text(json.dumps(chain, indent=2), encoding="utf-8")
    if corrects is not None:
        (folder / "corrections.json").write_text(
            json.dumps(corrections, indent=2), encoding="utf-8"
        )
    manifest = {
        "package_id": package_id,
        "tenant_id": db.tenant.tenant_id,
        "framework_id": framework_id,
        "valid_at": valid_at.isoformat(),
        "known_at": known.isoformat(),
        "corrects": corrects,
        "catalogue_version": catalogue.version(),
        "created_by": by,
        "created_at": datetime.now(UTC).isoformat(),
        "files": {p.name: sha256_file(p) for p in sorted(folder.iterdir())},
        "checkpoint": {"statement": point.statement, "signature": point.signature},
        "key_id": active.key_id,
        "public_key_pem": active.public_key_pem(),
    }
    signature = b64(active.sign(canonical(manifest)))
    (folder / MANIFEST).write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    (folder / SIGNATURE).write_text(signature, encoding="utf-8")
    sealer = store or checkpoints.store_for(settings)
    sealed = {
        p.name: sealer.put(f"packages/{db.tenant.tenant_id}/{package_id}/{p.name}", p.read_bytes())
        for p in sorted(folder.iterdir())
    }
    db.execute(
        "INSERT INTO audit_packages (tenant_id, package_id, framework_id, valid_at, known_at,"
        " corrects, manifest, signature, results, sealed_refs, created_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            db.tenant.tenant_id,
            package_id,
            framework_id,
            valid_at,
            known,
            corrects,
            json.dumps(manifest),
            signature,
            json.dumps(index),
            json.dumps(sealed),
            by,
        ),
    )
    return package_id, folder


package_app = typer.Typer(no_args_is_help=True, help="As-of audit packages: build, verify")


@package_app.command("build")
def build_cmd(
    framework: str = typer.Option(..., "--framework"),
    as_of: str = typer.Option(..., "--as-of", help="ISO time the package is about"),
    by: str = typer.Option(..., "--by"),
    corrects: str | None = typer.Option(None, "--corrects", help="the package this corrects"),
    tenant: str | None = typer.Option(None, "--tenant"),
) -> None:
    from ccp.storage import db as storage
    from ccp.tenancy import TenantContext

    settings = Settings()
    chosen = tenant or settings.ccp_tenant
    if not chosen:
        raise typer.BadParameter("pass --tenant or set CCP_TENANT")
    valid_at = datetime.fromisoformat(as_of)
    if valid_at.tzinfo is None:
        valid_at = valid_at.replace(tzinfo=UTC)
    pool = storage.app_pool(settings)
    try:
        scoped = ScopedDB(pool, TenantContext.for_id(settings, chosen))
        package_id, folder = build(scoped, settings, framework, valid_at, by=by, corrects=corrects)
        typer.echo(f"{package_id} {folder}")
    finally:
        pool.close()


@package_app.command("verify")
def verify_cmd(
    folder: Path = typer.Argument(...),
    public_key: Path | None = typer.Option(None, "--public-key"),
) -> None:
    """Check a package offline; exit 1 when anything does not verify."""
    from ccp.audit import package_verify

    pem = public_key.read_text(encoding="utf-8") if public_key else None
    report = package_verify.verify(folder, pem)
    typer.echo(json.dumps(report.__dict__, indent=2))
    if not report.ok:
        raise typer.Exit(1)
