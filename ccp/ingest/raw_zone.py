"""The immutable raw zone (spec.md Layer 2).

    data/raw/{tenant_id}/{connector}/{source_ts:%Y/%m/%d}/{source_event_id_safe}.json

Each file is the SourceEvent plus ``received_at`` and a sha256 of the payload, written to
a temporary name and renamed into place. A file is never opened for writing again. When
the primary path already exists, the new copy is written under a ``dups/`` sibling
directory instead: the raw zone records that the provider sent it twice.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ccp.connectors.base import SourceEvent, sha256_json

_SAFE = re.compile(r"[^A-Za-z0-9._@-]+")


@dataclass(frozen=True)
class LandedFile:
    path: Path
    payload_sha256: str
    duplicate_file: bool


def safe_id(source_event_id: str) -> str:
    cleaned = _SAFE.sub("_", source_event_id)
    return cleaned[:180]


class RawZone:
    def __init__(self, root: Path) -> None:
        self.root = root

    def primary_path(self, event: SourceEvent) -> Path:
        day = event.source_ts.strftime("%Y/%m/%d")
        return (
            self.root
            / event.tenant_id
            / event.connector
            / day
            / f"{safe_id(event.source_event_id)}.json"
        )

    def write(self, event: SourceEvent) -> LandedFile:
        received_at = datetime.now(UTC)
        primary = self.primary_path(event)
        if primary.exists():
            stamp = received_at.strftime("%Y%m%dT%H%M%S%fZ")
            target = primary.parent / "dups" / f"{primary.stem}.{stamp}.json"
            duplicate = True
        else:
            target = primary
            duplicate = False
        digest = sha256_json(event.payload)
        record: dict[str, Any] = {
            **event.model_dump(mode="json"),
            "received_at": received_at.isoformat(),
            "payload_sha256": digest,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, target)
        return LandedFile(path=target, payload_sha256=digest, duplicate_file=duplicate)

    def read(self, path: Path) -> SourceEvent:
        data = json.loads(path.read_text(encoding="utf-8"))
        data.pop("received_at", None)
        data.pop("payload_sha256", None)
        return SourceEvent.model_validate(data)
