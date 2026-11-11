"""MT5 (A-137): a per-tenant hash chain over the record, signed checkpoints, and a verify
that a restore drill cannot get past.

The defects: row grants stopped the application from editing the record, but not an
administrator or an altered backup, and nothing could tell afterwards that a row had
changed; there was no statement of the record's state that anyone outside the database could
hold CCP to.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import boto3
import psycopg
import pytest
from moto import mock_aws

from ccp.approvals import store as approvals
from ccp.audit import checkpoints, verify
from ccp.audit.signing import KmsSigner, LocalSigner, canonical
from ccp.config import Settings
from ccp.storage.db import app_pool, scratch_database
from ccp.storage.scoped import ScopedDB
from ccp.tenancy import TenantContext

pytestmark = pytest.mark.db


@pytest.fixture
def own(base_settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, ScopedDB, Path]]:
    try:
        with scratch_database(base_settings, prefix="ccp_chain") as settings:
            local = settings.model_copy(update={"ccp_data_dir": tmp_path / "data"})
            pool = app_pool(local, max_size=4)
            try:
                yield local, ScopedDB(pool, TenantContext.for_id(local, "acme")), tmp_path
            finally:
                pool.close()
    except psycopg.OperationalError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable: {exc}")


def _owner(settings: Settings) -> psycopg.Connection[Any]:
    return psycopg.connect(settings.database_admin_url, autocommit=True)


def _items(acme: ScopedDB, n: int, prefix: str = "draft") -> list[str]:
    return [approvals.open_item(acme, "policy_draft", f"{prefix}-{i}", {"n": i}) for i in range(n)]


def test_each_insert_is_chained_at_commit(own: tuple[Settings, ScopedDB, Path]) -> None:
    settings, acme, _ = own
    _items(acme, 2)
    rows = acme.fetch_all(
        "SELECT seq, table_name, prev_sha256, entry_sha256 FROM audit_chain ORDER BY seq"
    )
    assert [r[1] for r in rows] == ["approvals", "approval_events"] * 2
    assert [int(r[0]) for r in rows] == [1, 2, 3, 4]
    assert all(rows[i][2] == rows[i - 1][3] for i in range(1, len(rows)))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):  # only the trigger writes it
        acme.execute(
            "INSERT INTO audit_chain (tenant_id, seq, table_name, row_key, row_columns,"
            " row_sha256, prev_sha256, entry_sha256) VALUES ('acme', 99, 'x', '{}', '{}',"
            " 'x', 'x', 'x')"
        )
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        acme.fetch_one("SELECT * FROM audit_chain_heads")
    assert verify.verify(acme, settings).clean


def test_a_session_in_another_time_zone_hashes_the_same(
    own: tuple[Settings, ScopedDB, Path],
) -> None:
    settings, acme, _ = own
    with acme.transaction() as cur:
        cur.execute("SET LOCAL TimeZone = 'Asia/Kolkata'")
        cur.execute(
            "INSERT INTO run_stops (tenant_id, thread_id, requested_by) VALUES"
            " ('acme', 'acme:investigator:tz', 'issuer#ana')"
        )
    assert verify.verify(acme, settings).clean


def test_concurrent_writers_keep_one_unbroken_chain(own: tuple[Settings, ScopedDB, Path]) -> None:
    settings, acme, _ = own
    errors: list[BaseException] = []

    def write(prefix: str) -> None:
        try:
            _items(acme, 5, prefix)
        except BaseException as exc:  # noqa: BLE001 - reported below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(f"w{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    report = verify.verify(acme, settings)
    assert report.clean and report.entries == 30


def test_the_restore_drill_catches_an_altered_row(own: tuple[Settings, ScopedDB, Path]) -> None:
    settings, acme, _ = own
    _items(acme, 3)
    made = checkpoints.checkpoint(acme, settings)
    assert made is not None and made.seq == 6
    assert checkpoints.checkpoint(acme, settings) is None  # nothing moved: nothing to sign
    assert verify.verify(acme, settings).clean
    with _owner(settings) as owner:  # the drill: a row changed behind the application's back
        owner.execute(
            "UPDATE approvals SET payload = '{\"n\": 99}'::jsonb WHERE ref_id = 'draft-1'"
        )
    report = verify.verify(acme, settings)
    assert len(report.findings) == 1 and "approvals: the row of entry" in report.findings[0]
    assert "was altered" in report.findings[0]


def test_rewriting_rows_and_chain_together_still_fails_the_checkpoint(
    own: tuple[Settings, ScopedDB, Path],
) -> None:
    settings, acme, _ = own
    _items(acme, 2)
    assert checkpoints.checkpoint(acme, settings) is not None
    with _owner(settings) as owner:  # an administrator rewrites a row and re-hashes the chain
        owner.execute("SET TimeZone = 'UTC'")
        owner.execute("UPDATE approvals SET payload = '{\"n\": 7}'::jsonb WHERE ref_id = 'draft-0'")
        rows = owner.execute(
            "SELECT c.seq, c.table_name, (SELECT audit_row_sha256(to_jsonb(a)) FROM approvals a"
            " WHERE to_jsonb(a) @> c.row_key AND c.table_name = 'approvals')"
            " FROM audit_chain c WHERE c.tenant_id = 'acme' ORDER BY c.seq"
        ).fetchall()
        prev = "0" * 64
        for seq, table, fresh in rows:
            digest = (
                fresh
                or owner.execute(
                    "SELECT row_sha256 FROM audit_chain WHERE tenant_id = 'acme' AND seq = %s",
                    (seq,),
                ).fetchone()[0]
            )  # type: ignore[index]
            entry = hashlib.sha256(f"{prev}|{seq}|{table}|{digest}".encode()).hexdigest()
            owner.execute(
                "UPDATE audit_chain SET row_sha256 = %s, prev_sha256 = %s, entry_sha256 = %s"
                " WHERE tenant_id = 'acme' AND seq = %s",
                (digest, prev, entry, seq),
            )
            prev = entry
    report = verify.verify(acme, settings)
    assert report.findings == [
        f"checkpoint 4 ({checkpoints.latest(acme).signed_at:%Y-%m-%d %H:%M}): the chain no"  # type: ignore[union-attr]
        " longer has the entry it signed"
    ]


def test_a_missing_or_unchained_row_and_a_forged_export_are_found(
    own: tuple[Settings, ScopedDB, Path],
) -> None:
    settings, acme, _ = own
    _items(acme, 2)
    made = checkpoints.checkpoint(acme, settings)
    assert made is not None
    with _owner(settings) as owner:
        owner.execute(
            "DELETE FROM approval_events WHERE tenant_id = 'acme' AND event = 'opened'"
            " AND approval_id = (SELECT approval_id FROM approvals WHERE ref_id = 'draft-0')"
        )
        owner.execute("SET session_replication_role = replica")  # triggers off: unchained
        owner.execute(
            "INSERT INTO run_stops (tenant_id, thread_id, requested_by) VALUES"
            " ('acme', 'acme:investigator:ghost', 'nobody')"
        )
    Path(made.exported_to).write_text(
        json.dumps({"statement": {**made.statement, "seq": 1}, "signature": made.signature})
    )
    findings = verify.verify(acme, settings).findings
    assert any("approval_events: the row of entry" in f and "missing" in f for f in findings)
    assert any("run_stops: 1 rows were never chained" in f for f in findings)
    assert any("the export says other than the database" in f for f in findings)


def test_signatures_verify_and_a_forgery_does_not(tmp_path: Path) -> None:
    signer = LocalSigner(tmp_path / "key.pem")
    again = LocalSigner(tmp_path / "key.pem")  # the same key, read back
    message = canonical({"seq": 1, "entry_sha256": "ab"})
    signature = signer.sign(message)
    assert again.key_id == signer.key_id and again.verify(message, signature)
    assert not signer.verify(canonical({"seq": 2, "entry_sha256": "ab"}), signature)
    with mock_aws():
        kms = boto3.client("kms", region_name="us-east-1")
        key = kms.create_key(KeyUsage="SIGN_VERIFY", KeySpec="ECC_NIST_P256")["KeyMetadata"]
        remote = KmsSigner(key["KeyId"], kms)
        signed = remote.sign(message)
        assert remote.verify(message, signed)
        assert not remote.verify(canonical({"seq": 3}), signed)


def test_the_export_bucket_is_locked_in_compliance_mode() -> None:
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="ccp-audit", ObjectLockEnabledForBucket=True)
        store = checkpoints.S3CheckpointStore("ccp-audit", s3, retain_days=1)
        ref = store.put("acme/000000000001.json", b'{"statement": {}}')
        assert store.get(ref) == b'{"statement": {}}'
        key, version = "acme/000000000001.json", ref.rsplit("=", 1)[1]
        head = s3.head_object(Bucket="ccp-audit", Key=key, VersionId=version)
        assert head["ObjectLockMode"] == "COMPLIANCE"
        with pytest.raises(s3.exceptions.ClientError, match="AccessDenied"):
            s3.delete_object(
                Bucket="ccp-audit", Key=key, VersionId=version, BypassGovernanceRetention=True
            )
