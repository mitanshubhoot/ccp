"""NT3 (A-96): a manifest entry is read only from inside the tenant's upload folder. Parent
references, absolute paths, drive-relative paths, and links that leave the folder are
refused with a `manual.refused` event that maps to a guardrail event and no evidence; a
contained subfolder still works."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ccp.config import Settings
from ccp.connectors.manual.pdf_text import PdfLimits
from ccp.connectors.manual.uploads import ManualUploadConnector, contained_path
from ccp.normalize.manual_mapper import map_manual_event
from ccp.tenancy import TenantContext

SECRET = "globex payroll export: account 4410-22, salary bands attached\n"


def _manifest(name: str) -> str:
    return (
        "uploads:\n"
        f"  - file: {name!r}\n"
        "    evidence_type: policy_document\n"
        "    title: Planted\n"
        "    issued_at: 2026-01-01T00:00:00Z\n"
        "    uploaded_at: 2026-09-01T00:00:00Z\n"
    )


def _layout(tmp_path: Path) -> tuple[Path, Path]:
    acme = tmp_path / "acme"
    acme.mkdir()
    other = tmp_path / "globex"
    other.mkdir()
    (other / "secret.md").write_text(SECRET, encoding="utf-8")
    return acme, other / "secret.md"


def _pull(tmp_path: Path, settings: Settings) -> list:  # type: ignore[type-arg]
    connector = ManualUploadConnector(lambda t: tmp_path / t, PdfLimits())
    return list(connector.pull(TenantContext.for_id(settings, "acme")))


@pytest.mark.parametrize(
    "name",
    [
        "../globex/secret.md",
        "sub/../../globex/secret.md",
        "ABSOLUTE",
        pytest.param(
            "..\\globex\\secret.md",
            marks=pytest.mark.skipif(os.name != "nt", reason="a separator on Windows only"),
        ),
        pytest.param(
            "C:secret.md",
            marks=pytest.mark.skipif(os.name != "nt", reason="drive-relative on Windows only"),
        ),
    ],
)
def test_a_path_outside_the_tenant_folder_is_refused(
    tmp_path: Path, base_settings: Settings, name: str
) -> None:
    acme, secret = _layout(tmp_path)
    entry = str(secret.resolve()) if name == "ABSOLUTE" else name
    (acme / "manifest.yaml").write_text(_manifest(entry), encoding="utf-8")
    events = _pull(tmp_path, base_settings)
    assert [e.event_type for e in events] == ["manual.refused"]
    assert "text" not in events[0].payload and SECRET.strip() not in str(events[0].payload)
    mapped = map_manual_event(events[0])
    assert mapped.entities == ()
    assert [g.split(":")[0] for g in mapped.guardrail_events] == ["upload_refused"]
    assert "path_outside_tenant_folder" in mapped.guardrail_events[0]


def test_a_link_that_leaves_the_tenant_folder_is_refused(
    tmp_path: Path, base_settings: Settings
) -> None:
    acme, secret = _layout(tmp_path)
    link = acme / "linked.md"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("creating a symbolic link needs a privilege this machine does not grant")
    (acme / "manifest.yaml").write_text(_manifest("linked.md"), encoding="utf-8")
    events = _pull(tmp_path, base_settings)
    assert [e.event_type for e in events] == ["manual.refused"]


def test_a_contained_subfolder_is_still_read(tmp_path: Path, base_settings: Settings) -> None:
    acme, _ = _layout(tmp_path)
    (acme / "notes").mkdir()
    (acme / "notes" / "ok.md").write_text("# Access policy\n\nReviewed quarterly.\n", "utf-8")
    (acme / "manifest.yaml").write_text(_manifest("notes/ok.md"), encoding="utf-8")
    events = _pull(tmp_path, base_settings)
    assert [e.event_type for e in events] == ["manual.document"]
    assert "Reviewed quarterly" in events[0].payload["text"]


def test_contained_path_rules(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    root.mkdir()
    (root / "a.md").write_text("x", encoding="utf-8")
    assert contained_path(root, "a.md") == (root / "a.md").resolve()
    for bad in ("", ".", "..", "../x", "a/../../x", str((tmp_path / "x").resolve())):
        assert contained_path(root, bad) is None, bad
