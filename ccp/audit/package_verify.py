"""Verify an audit package offline (spec.md A-168): no database, no network, no CCP.

An auditor or a customer runs this on a package folder, ideally with CCP's public key
obtained apart from the package (`--public-key`); without one, the key the package carries
is used and the result says the key was not pinned. It checks, in order:

    files       every file the manifest lists has the digest the manifest gives
    manifest    the manifest's signature, with the key
    checkpoint  the signed checkpoint the package rests on: the tenant's chain head at a
                sequence number, signed with the same key
    chain       the chain segment recomputes, entry by entry (sha256 of the previous entry,
                the sequence number, the table and the row digest), to that signed head
    results     every result's row is in the segment, the row's text hashes to its digest,
                and the row says what the result says (verdict, test, subject, status)

Only the Python standard library and `cryptography` are needed, so this file can be
handed to an auditor on its own.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MANIFEST = "manifest.json"
SIGNATURE = "manifest.sig"


def canonical(document: dict[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def entry_of(prev: str, seq: int, table: str, digest: str) -> str:
    return hashlib.sha256(f"{prev}|{seq}|{table}|{digest}".encode()).hexdigest()


def check_signature(public_pem: str, message: bytes, signature_b64: str) -> bool:
    key = serialization.load_pem_public_key(public_pem.encode("utf-8"))
    signature = base64.b64decode(signature_b64)
    try:
        if isinstance(key, Ed25519PublicKey):
            key.verify(signature, message)
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(signature, message, ec.ECDSA(hashes.SHA256()))
        else:
            return False
    except InvalidSignature:
        return False
    return True


@dataclass
class Report:
    ok: bool = True
    pinned: bool = False
    checked: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    def fail(self, problem: str) -> None:
        self.ok = False
        self.problems.append(problem)


def verify(folder: Path, public_key_pem: str | None = None) -> Report:
    report = Report()
    manifest_path = folder / MANIFEST
    if not manifest_path.is_file():
        report.fail("no manifest.json")
        return report
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, digest in sorted(manifest.get("files", {}).items()):
        path = folder / name
        if not path.is_file():
            report.fail(f"file missing: {name}")
        elif sha256_file(path) != digest:
            report.fail(f"file altered: {name}")
    report.checked["files"] = len(manifest.get("files", {}))

    key = public_key_pem or manifest.get("public_key_pem", "")
    report.pinned = public_key_pem is not None
    signature = (
        (folder / SIGNATURE).read_text(encoding="utf-8").strip()
        if (folder / SIGNATURE).is_file()
        else ""
    )
    if not key or not check_signature(key, canonical(manifest), signature):
        report.fail("the manifest's signature does not verify with the key")

    point = manifest.get("checkpoint") or {}
    statement = point.get("statement") or {}
    if not check_signature(key, canonical(statement), point.get("signature", "")):
        report.fail("the checkpoint's signature does not verify with the key")
    if statement.get("tenant_id") != manifest.get("tenant_id"):
        report.fail("the checkpoint is another tenant's")

    chain = (
        json.loads((folder / "chain.json").read_text(encoding="utf-8"))
        if (folder / "chain.json").is_file()
        else []
    )
    if chain:
        prev = chain[0]["prev_sha256"]
        for link in chain:
            if link["prev_sha256"] != prev:
                report.fail(f"chain break before seq {link['seq']}")
                break
            computed = entry_of(prev, int(link["seq"]), link["table_name"], link["row_sha256"])
            if computed != link["entry_sha256"]:
                report.fail(f"chain entry {link['seq']} does not recompute")
                break
            prev = computed
        if int(chain[-1]["seq"]) != int(statement.get("seq", -1)) or prev != statement.get(
            "entry_sha256"
        ):
            report.fail("the chain segment does not end at the signed checkpoint")
    report.checked["chain_entries"] = len(chain)

    by_seq = {int(link["seq"]): link for link in chain}
    results = (
        json.loads((folder / "results.json").read_text(encoding="utf-8"))
        if (folder / "results.json").is_file()
        else []
    )
    for result in results:
        proof = result.get("proof") or {}
        link = by_seq.get(int(proof.get("seq", -1)))
        label = f"{result.get('test_id')} {result.get('subject_key')}"
        if link is None:
            report.fail(f"{label}: its row is not in the chain segment")
            continue
        row_text = str(proof.get("row_text", ""))
        if hashlib.sha256(row_text.encode("utf-8")).hexdigest() != link["row_sha256"]:
            report.fail(f"{label}: the row does not hash to its chained digest")
            continue
        row = json.loads(row_text)
        for name in ("verdict_id", "test_id", "subject_key", "status"):
            if name in row and str(row[name]) != str(result.get(name)):
                report.fail(f"{label}: the chained row says {name}={row[name]}")
    report.checked["results"] = len(results)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify a CCP audit package offline.")
    parser.add_argument("folder", type=Path)
    parser.add_argument(
        "--public-key", type=Path, help="CCP's public key (PEM), got apart from the package"
    )
    args = parser.parse_args(argv)
    pem = args.public_key.read_text(encoding="utf-8") if args.public_key else None
    report = verify(args.folder, pem)
    print(json.dumps(report.__dict__, indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
