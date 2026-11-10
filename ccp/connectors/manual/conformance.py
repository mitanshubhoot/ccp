"""The upload folder's conformance suite (spec.md A-122).

The upload connector has no network, but its live source reads whatever a tenant put in
its folder, so it keeps the same promises: an entry it does not read leaves the scope
partial, a folder it cannot read stops the pull with a named kind, and the same files
pulled again are the same events. Each scenario writes a folder under a temporary root.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path

from ccp.connectors.base import Connector, SourceEvent, SyncReport
from ccp.connectors.conformance import Control, Expect, Scenario, Sleep
from ccp.connectors.manual.pdf_text import PdfLimits
from ccp.connectors.manual.uploads import ManualUploadConnector
from ccp.tenancy import TenantContext

_ENTRY = """  - file: {file}
    evidence_type: policy_document
    title: {title}
    issued_at: "2026-09-01T00:00:00+00:00"
    uploaded_at: "2026-09-02T00:00:00+00:00"
"""
_POLICY = "---\ntitle: Access policy\n---\nAccess is reviewed every quarter.\n"

# scenario -> (manifest entries, files on disk); None: no manifest at all
FOLDERS: dict[str, tuple[list[str], dict[str, str]] | None] = {
    "normal": (["access-policy.md"], {"access-policy.md": _POLICY}),
    "path-outside-the-folder": (
        ["access-policy.md", "../elsewhere.md"],
        {"access-policy.md": _POLICY},
    ),
    "listed-file-missing": (["access-policy.md", "gone.md"], {"access-policy.md": _POLICY}),
    "unsupported-type": (
        ["access-policy.md", "diagram.png"],
        {"access-policy.md": _POLICY, "diagram.png": "not an image"},
    ),
    "no-manifest": None,
}


def scenarios() -> tuple[Scenario, ...]:
    return (
        Scenario(
            "normal",
            "one policy, listed and present",
            Expect("complete", None, {"uploads": "complete"}),
            repeatable=True,
        ),
        Scenario(
            "path-outside-the-folder",
            "a manifest entry that climbs out of the tenant's folder is refused",
            Expect("partial", None, {"uploads": "partial"}),
        ),
        Scenario(
            "listed-file-missing",
            "a manifest entry with no file on disk",
            Expect("partial", None, {"uploads": "partial"}),
        ),
        Scenario(
            "unsupported-type",
            "a listed file of a type the connector cannot read",
            Expect("partial", None, {"uploads": "partial"}),
        ),
        Scenario("no-manifest", "a folder with no manifest", Expect(stops="fail")),
    )


class Suite:
    connector = "manual"

    def __init__(self, cls: type[ManualUploadConnector] = ManualUploadConnector) -> None:
        self._cls = cls
        self._root: Path | None = None
        self.scenarios: tuple[Scenario, ...] = scenarios()

    def _folder(self, scenario: Scenario) -> Path:
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix="ccp-conformance-"))
        folder = self._root / scenario.name
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        spec = FOLDERS[scenario.name]
        if spec is not None:
            entries, files = spec
            manifest = "uploads:\n" + "".join(
                _ENTRY.format(file=name, title=name) for name in entries
            )
            (folder / "manifest.yaml").write_text(manifest, encoding="utf-8")
            for name, text in files.items():
                (folder / name).write_text(text, encoding="utf-8")
        return folder

    def build(self, scenario: Scenario, sleep: Sleep) -> Connector:
        folder = self._folder(scenario)
        limits = PdfLimits(max_bytes=1_000_000, max_pages=10, timeout_seconds=5.0)
        return self._cls(lambda tenant_id: folder, limits, "live")

    def settled(self, events: list[SourceEvent]) -> set[str] | None:
        return None  # an upload that is not there revokes nothing

    def recordings(self) -> Iterable[str]:
        return [_POLICY, *(_ENTRY.format(file=n, title=n) for n in ("a.md", "b.md"))]

    def close(self) -> None:
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)
            self._root = None


class _LeftOutIgnored(ManualUploadConnector):
    """Claims the folder read in full whatever it left out."""

    def pull(
        self, tenant: TenantContext, since: datetime | None = None, report: SyncReport | None = None
    ) -> Iterator[SourceEvent]:
        def events() -> Iterator[SourceEvent]:
            yield from super(_LeftOutIgnored, self).pull(tenant, since, report)
            if report is not None:
                report.complete("uploads", 0)

        return events()


def controls() -> list[Control]:
    return [
        Control(
            "uploads: left-out entries ignored",
            "a scope with entries not read is never complete",
            Suite(_LeftOutIgnored),
        )
    ]
