"""MT5 (A-138): the infrastructure as code holds the settings the record depends on.

Nothing here applies Terraform (that waits for AWS credentials). The files are parsed and
the settings A-136 and A-137 rely on are pinned: the raw bucket versioned and locked in
governance mode under per-tenant KMS keys, the audit bucket and signing key in the separate
account in compliance mode, RDS PostgreSQL 17 Multi-AZ, encrypted, restorable and
protected, a task role that cannot delete or bypass retention, and an environment that
names only settings CCP has.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import hcl2
import yaml

from ccp.config import Settings
from ccp.ingest.raw_zone import S3RawZone

ROOT = Path(__file__).resolve().parents[2]
TF = ROOT / "infra" / "terraform"


def _unquote(text: str) -> str:
    return text[1:-1] if len(text) >= 2 and text[0] == text[-1] == '"' else text


def _clean(value: Any) -> Any:
    """python-hcl2 keeps HCL's quotes on keys and strings; compare without them."""
    if isinstance(value, dict):
        return {_unquote(k): _clean(v) for k, v in value.items() if k != "__is_block__"}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return _unquote(value) if isinstance(value, str) else value


def _resources() -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for path in sorted(TF.glob("*.tf")):
        with path.open(encoding="utf-8") as handle:
            parsed = _clean(hcl2.load(handle))
        for block in parsed.get("resource", []):
            for kind, named in block.items():
                for name, body in named.items():
                    out.setdefault(kind, {})[name] = body
    return out


def _one(value: Any) -> Any:
    return value[0] if isinstance(value, list) and len(value) == 1 else value


def test_the_raw_bucket_is_versioned_locked_and_encrypted() -> None:
    r = _resources()
    assert r["aws_s3_bucket"]["raw"]["object_lock_enabled"] is True
    assert (
        _one(r["aws_s3_bucket_versioning"]["raw"]["versioning_configuration"])["status"]
        == "Enabled"
    )
    rule = _one(r["aws_s3_bucket_object_lock_configuration"]["raw"]["rule"])
    assert _one(rule["default_retention"])["mode"] == "GOVERNANCE"
    sse = _one(r["aws_s3_bucket_server_side_encryption_configuration"]["raw"]["rule"])
    assert sse["bucket_key_enabled"] is True
    assert _one(sse["apply_server_side_encryption_by_default"])["sse_algorithm"] == "aws:kms"
    block = r["aws_s3_bucket_public_access_block"]["raw"]
    assert all(
        block[k] is True
        for k in (
            "block_public_acls",
            "block_public_policy",
            "ignore_public_acls",
            "restrict_public_buckets",
        )
    )
    alias = r["aws_kms_alias"]["raw"]["name"]
    assert alias.replace("${each.key}", "{tenant}") == S3RawZone("b", None).kms_alias


def test_the_audit_account_locks_checkpoints_and_holds_the_signing_key() -> None:
    r = _resources()
    for kind in ("aws_s3_bucket", "aws_s3_bucket_object_lock_configuration"):
        assert r[kind]["audit"]["provider"] == "${aws.audit}"
    rule = _one(r["aws_s3_bucket_object_lock_configuration"]["audit"]["rule"])
    assert _one(rule["default_retention"])["mode"] == "COMPLIANCE"
    key = r["aws_kms_key"]["audit_signing"]
    assert key["provider"] == "${aws.audit}"
    assert (key["key_usage"], key["customer_master_key_spec"]) == ("SIGN_VERIFY", "ECC_NIST_P256")


def test_the_database_is_postgres_17_multi_az_encrypted_and_restorable() -> None:
    db = _resources()["aws_db_instance"]["ccp"]
    assert (db["engine"], db["engine_version"]) == ("postgres", "17")
    assert db["multi_az"] is True and db["storage_encrypted"] is True
    assert db["deletion_protection"] is True and db["publicly_accessible"] is False
    assert db["backup_retention_period"] >= 7


def test_the_task_role_cannot_delete_or_bypass_retention() -> None:
    text = (TF / "services.tf").read_text(encoding="utf-8")
    deny = re.search(r'sid\s*=\s*"NoDelete"(.*?)\n  }', text, re.S)
    assert deny is not None and 'effect    = "Deny"' in deny.group(1)
    for action in ("s3:DeleteObject", "s3:DeleteObjectVersion", "s3:BypassGovernanceRetention"):
        assert f'"{action}"' in deny.group(1)


def test_the_environment_names_only_settings_ccp_has() -> None:
    text = (TF / "services.tf").read_text(encoding="utf-8")
    names = set(re.findall(r'name = "([A-Z_]+)"', text)) | set(
        re.findall(r'"([A-Z][A-Z_]+)"', text)
    )
    fields = {f.upper() for f in Settings.model_fields}
    unknown = {n for n in names if n.startswith(("CCP_", "DATABASE_")) and n not in fields}
    assert unknown == set()


def test_the_deploy_gates_on_the_tests_and_the_eval_regression() -> None:
    flow = yaml.safe_load((ROOT / ".github" / "workflows" / "deploy.yml").read_text("utf-8"))
    steps = [s.get("run", "") for s in flow["jobs"]["gate"]["steps"]]
    assert any("pytest" in s for s in steps)
    assert any("ccp evals regress --strict" in s for s in steps)
    assert flow["jobs"]["deploy"]["needs"] == ["gate"]
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER ccp" in dockerfile and "uv sync --frozen --no-dev" in dockerfile
