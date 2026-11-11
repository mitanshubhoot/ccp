"""Manual upload connector (spec.md Layer 1, "Upload Evidence" and "Upload / Create Policies").

The tenant is taken from the directory the files sit in and from nothing else. A manifest
is required; a file present in the directory but absent from the manifest is logged and
ignored (A-29); a manifest entry whose file is missing is logged and skipped. Anything a
manifest entry says about a tenant is carried in the payload for the normalization layer
to reject with a guardrail event (S13), never acted on here. A manifest entry is read only
from inside the tenant's folder: a parent reference, an absolute or drive path, or a link
that resolves elsewhere yields a `manual.refused` event and nothing is read (A-96).

A scan is kept for transcription (A-147): an image (PNG, JPEG, GIF or WebP, known by its
first bytes) or a PDF with pages that have no text layer is landed as bytes in the raw zone
(`responses`, content-addressed), and its event names the digest and the blank pages, so
the transcription job reads exactly what was landed. Nothing here calls a model.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from ccp.connectors.base import (
    ConnectorName,
    PullError,
    Received,
    SourceEvent,
    SourceKind,
    SyncReport,
    after,
    sha256_bytes,
)
from ccp.connectors.frontmatter import split_front_matter
from ccp.connectors.manual.pdf_text import PdfLimits, blank_pages, extract_pages, image_type
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


class ManifestMissing(PullError):
    """A folder with no manifest cannot be read at all: the pull stops, as a failure (A-122)."""


class ManualUploadConnector:
    name: ConnectorName = "manual"
    scopes: tuple[str, ...] = ("uploads",)

    def __init__(
        self,
        root_for_tenant: Callable[[TenantId], Path],
        limits: PdfLimits,
        source: SourceKind = "fixture",
    ) -> None:
        self._root_for_tenant = root_for_tenant
        self._limits = limits
        self.source: SourceKind = source
        self.responses: list[Received] = []  # scans kept for transcription (A-147)

    def pull(
        self,
        tenant: TenantContext,
        since: datetime | None = None,
        report: SyncReport | None = None,
    ) -> Iterator[SourceEvent]:
        return after(self._events(tenant, report, since), since)

    def _keep(self, entry: ManifestEntry, data: bytes, since: datetime | None) -> None:
        if since is None or entry.uploaded_at > since:
            self.responses.append(
                Received("UPLOAD", f"uploads/{entry.file}", 200, None, data, datetime.now(UTC))
            )

    def _events(
        self,
        tenant: TenantContext,
        report: SyncReport | None = None,
        since: datetime | None = None,
    ) -> Iterator[SourceEvent]:
        self.responses = []
        read, left_out = 0, []  # entries not read: refused, missing, unsupported (A-118)
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
                left_out.append(f"refused {entry.file[:80]}")
                continue
            if not path.is_file():
                log.warning(
                    "manifest entry %s/%s has no file on disk; skipped",
                    tenant.tenant_id,
                    entry.file,
                )
                left_out.append(f"missing {entry.file}")
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
                blanks = (
                    blank_pages(extraction.pages)
                    if extraction.status in ("ok", "no_text_layer")
                    else []
                )
                if blanks:
                    payload["pdf"]["blank_pages"] = blanks
                    payload["attachment"] = {"sha256": digest, "media_type": "application/pdf"}
                    self._keep(entry, data, since)
            elif (media_type := image_type(data)) is not None:
                if len(data) > self._limits.max_bytes:
                    left_out.append(f"too large {entry.file}")
                    continue
                payload["image"] = {"media_type": media_type}
                payload["attachment"] = {"sha256": digest, "media_type": media_type}
                payload["text"] = ""
                self._keep(entry, data, since)
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
                left_out.append(f"unsupported {entry.file}")
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
            read += 1
        if report is not None:
            if left_out:
                report.mark("uploads", "partial", read, "; ".join(left_out)[:2000])
            else:
                report.complete("uploads", read)
