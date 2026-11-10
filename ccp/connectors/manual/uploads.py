"""Manual upload connector (spec.md Layer 1, "Upload Evidence" and "Upload / Create Policies").

The tenant is taken from the directory the files sit in and from nothing else. A manifest
is required; a file present in the directory but absent from the manifest is logged and
ignored (A-29); a manifest entry whose file is missing is logged and skipped. Anything a
manifest entry says about a tenant is carried in the payload for the normalization layer
to reject with a guardrail event (S13), never acted on here. A manifest entry is read only
from inside the tenant's folder: a parent reference, an absolute or drive path, or a link
that resolves elsewhere yields a `manual.refused` event and nothing is read (A-96).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from ccp.connectors.base import ConnectorName, SourceEvent, after, sha256_bytes
from ccp.connectors.frontmatter import split_front_matter
from ccp.connectors.manual.pdf_text import PdfLimits, extract_pages
from ccp.tenancy import TenantContext, TenantId
from ccp.types import EvidenceType

log = logging.getLogger(__name__)
SCHEMA_VERSION = "manual.v1"
_TEXT_SUFFIXES = {".md", ".txt"}
REFUSED_OUTSIDE = "path_outside_tenant_folder"


def contained_path(root: Path, name: str) -> Path | None:
    """The file a manifest entry names, resolved inside ``root``, or None when the name is
    absolute, carries a drive or a parent reference, or resolves (through a link) elsewhere."""
    relative = Path(name)
    if not name or relative.is_absolute() or relative.drive or relative.root:
        return None
    if ".." in relative.parts:
        return None
    base = root.resolve()
    target = (base / relative).resolve()
    if target == base or not target.is_relative_to(base):
        return None
    return target


class ManifestEntry(BaseModel):
    """One upload. Extra keys (such as a tenant claim) are kept so the mapper can log them."""

    model_config = ConfigDict(extra="allow")

    file: str
    evidence_type: EvidenceType
    title: str
    issued_at: datetime
    uploaded_at: datetime
    subject_employee_id: str | None = None

    @field_validator("issued_at", "uploaded_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("manifest timestamps must carry a timezone")
        return value


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    uploads: list[ManifestEntry]


class ManifestMissing(FileNotFoundError):
    pass


class ManualUploadConnector:
    name: ConnectorName = "manual"

    def __init__(self, root_for_tenant: Callable[[TenantId], Path], limits: PdfLimits) -> None:
        self._root_for_tenant = root_for_tenant
        self._limits = limits

    def pull(self, tenant: TenantContext, since: datetime | None = None) -> Iterator[SourceEvent]:
        return after(self._events(tenant), since)

    def _events(self, tenant: TenantContext) -> Iterator[SourceEvent]:
        root = self._root_for_tenant(tenant.tenant_id)
        manifest_path = root / "manifest.yaml"
        if not manifest_path.is_file():
            raise ManifestMissing(f"uploads for {tenant.tenant_id!r} need {manifest_path}")
        manifest = Manifest.model_validate(
            yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        )
        listed = {entry.file for entry in manifest.uploads}
        for stray in sorted(
            p.name for p in root.iterdir() if p.is_file() and p.name != "manifest.yaml"
        ):
            if stray not in listed:
                log.warning("upload %s/%s is not in the manifest; ignored", tenant.tenant_id, stray)

        for entry in manifest.uploads:
            path = contained_path(root, entry.file)
            if path is None:
                log.warning(
                    "manifest entry %s/%r points outside the tenant's upload folder; refused",
                    tenant.tenant_id,
                    entry.file,
                )
                yield SourceEvent(
                    tenant_id=tenant.tenant_id,
                    connector="manual",
                    source_event_id=(
                        f"upload-refused:{sha256_bytes(entry.file.encode('utf-8'))[:16]}"
                    ),
                    source_ts=entry.uploaded_at,
                    event_type="manual.refused",
                    payload={"filename": entry.file[:200], "reason": REFUSED_OUTSIDE},
                    schema_version=SCHEMA_VERSION,
                )
                continue
            if not path.is_file():
                log.warning(
                    "manifest entry %s/%s has no file on disk; skipped",
                    tenant.tenant_id,
                    entry.file,
                )
                continue
            data = path.read_bytes()
            digest = sha256_bytes(data)
            payload: dict[str, Any] = {
                "filename": entry.file,
                "sha256": digest,
                "size_bytes": len(data),
                "manifest": entry.model_dump(mode="json"),
                "evidence_type": entry.evidence_type.value,
                "subject_employee_id": entry.subject_employee_id,
                "title": entry.title,
                "issued_at": entry.issued_at.isoformat(),
            }
            suffix = path.suffix.lower()
            if suffix == ".pdf":
                extraction = extract_pages(path, self._limits)
                payload["pdf"] = {
                    "status": extraction.status,
                    "page_count": extraction.page_count,
                    "pages": extraction.pages,
                    "detail": extraction.detail,
                }
                payload["text"] = extraction.text
            elif suffix in _TEXT_SUFFIXES:
                front_matter, body = split_front_matter(data.decode("utf-8"))
                payload["front_matter"] = {
                    k: (v if isinstance(v, str | int | float | bool | None) else str(v))
                    for k, v in front_matter.items()
                }
                payload["text"] = body
            else:
                log.warning(
                    "upload %s/%s has unsupported type %s; skipped",
                    tenant.tenant_id,
                    entry.file,
                    suffix,
                )
                continue
            yield SourceEvent(
                tenant_id=tenant.tenant_id,
                connector="manual",
                source_event_id=f"upload:{entry.file}:{digest[:12]}",
                source_ts=entry.uploaded_at,
                event_type="manual.document",
                payload=payload,
                schema_version=SCHEMA_VERSION,
            )
