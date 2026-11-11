"""LT4 (A-168): an as-of audit package an auditor can verify offline. Each result carries
its method, raw pointers and a proof that its verdict row sits in the tenant's hash chain
under a signed checkpoint; the manifest is signed; a correction is a delta.

The defect: proofs existed only inside CCP, so an auditor had to trust whatever the
product showed them on the day.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ulid import ULID

from ccp.audit import package_verify
from ccp.audit.checkpoints import LocalCheckpointStore
from ccp.audit.signing import LocalSigner
from ccp.compliance_state import packages
from ccp.compliance_state import writer as state_writer
from ccp.config import Settings
from ccp.evaluators import runner as evaluations
from ccp.evaluators.verdict import latest_per_subject, persist
from ccp.knowledge import graph as knowledge
from ccp.storage.scoped import ScopedDB
from ccp.types import VerdictStatus
from tests.support.pipeline import prepared_tenants

pytestmark = pytest.mark.db
NOW = datetime(2026, 9, 11, 12, tzinfo=UTC)


@pytest.fixture(scope="module")
def world(base_settings: Settings) -> Iterator[tuple[Settings, ScopedDB]]:
    with prepared_tenants(base_settings, "ccp_packages", ("acme",)) as (settings, dbs):
        acme = dbs["acme"]
        evaluations.enqueue_all(acme, settings, now=NOW, tests=("HR-1", "GH-1"))
        evaluations.drain(acme, settings, now=NOW)
        state_writer.drain(acme)
        yield settings, acme


def _build(
    settings: Settings, acme: ScopedDB, tmp: Path, **kw: object
) -> tuple[str, Path, LocalSigner]:
    signer = LocalSigner(tmp / "key.pem")
    store = LocalCheckpointStore(tmp / "sealed")
    package_id, folder = packages.build(
        acme,
        settings,
        "soc2",
        NOW + timedelta(hours=1),
        by="sam",
        signer=signer,
        store=store,
        out_dir=tmp / "out",
        **kw,  # type: ignore[arg-type]
    )
    return package_id, folder, signer


def test_a_package_verifies_offline_and_tampering_is_caught(
    world: tuple[Settings, ScopedDB], tmp_path: Path
) -> None:
    settings, acme = world
    package_id, folder, signer = _build(settings, acme, tmp_path)
    results = json.loads((folder / "results.json").read_text(encoding="utf-8"))
    assert results and all(r["proof"] and r["method"]["tier"] == 1 for r in results)
    assert {r["test_id"] for r in results} >= {"HR-1"}
    assert any(e["raw_ref"] and e["stored_sha256"] for r in results for e in r["evidence"])
    report = package_verify.verify(folder, signer.public_key_pem())
    assert report.ok and report.pinned, report.problems
    assert report.checked["results"] == len(results) and report.checked["chain_entries"] > 0
    assert (tmp_path / "sealed" / "packages" / "acme" / package_id / "manifest.json").is_file()
    with pytest.raises(FileExistsError):  # sealed: written once
        LocalCheckpointStore(tmp_path / "sealed").put(
            f"packages/acme/{package_id}/manifest.json", b"{}"
        )

    unpinned = package_verify.verify(folder)
    assert unpinned.ok and not unpinned.pinned
    stranger = LocalSigner(tmp_path / "other.pem")
    assert not package_verify.verify(folder, stranger.public_key_pem()).ok

    results[0]["status"] = "pass" if results[0]["status"] != "pass" else "fail"
    (folder / "results.json").write_text(json.dumps(results), encoding="utf-8")
    altered = package_verify.verify(folder, signer.public_key_pem())
    assert "file altered: results.json" in altered.problems
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["results.json"] = package_verify.sha256_file(folder / "results.json")
    (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    covered = package_verify.verify(folder, signer.public_key_pem())
    assert any("signature" in p for p in covered.problems)  # the digest was moved, the seal holds
    assert any("chained row says status" in p for p in covered.problems)


def test_a_correction_is_a_delta_against_the_package_it_corrects(
    world: tuple[Settings, ScopedDB], tmp_path: Path
) -> None:
    settings, acme = world
    first, _, _ = _build(settings, acme, tmp_path / "a")
    same, folder, signer = _build(settings, acme, tmp_path / "b", corrects=first)
    assert json.loads((folder / "results.json").read_text(encoding="utf-8")) == []
    assert package_verify.verify(folder, signer.public_key_pem()).ok

    old = next(v for v in latest_per_subject(acme, "HR-1") if v.status == VerdictStatus.failed)
    later = NOW + timedelta(minutes=30)
    fixed = old.model_copy(
        update={
            "verdict_id": str(ULID()),
            "status": VerdictStatus.passed,
            "reasoning": "access removed (a corrected reading)",
            "trigger_observed_at": later,
            "valid_from": later,
            "evaluation_key": f"{old.evaluation_key}:corrected",
        }
    )
    assert persist(acme, fixed)
    assert state_writer.write_verdict(acme, fixed, knowledge.load()) is not None
    delta, folder, signer = _build(settings, acme, tmp_path / "c", corrects=first)
    corrections = json.loads((folder / "corrections.json").read_text(encoding="utf-8"))
    assert [c["subject_key"] for c in corrections] == [old.subject_key]
    assert (
        corrections[0]["before"]["status"] == "fail" and corrections[0]["after"]["status"] == "pass"
    )
    assert package_verify.verify(folder, signer.public_key_pem()).ok
    with pytest.raises(ValueError, match="no package"):
        _build(settings, acme, tmp_path / "d", corrects="nope")
