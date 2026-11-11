"""Signing checkpoints (spec.md A-137).

    LocalSigner   an Ed25519 key in the data directory: offline and in tests
    KmsSigner     an asymmetric AWS KMS key (ECC_NIST_P256, ECDSA_SHA_256): the private key
                  never leaves KMS, and the database's administrator does not hold it

A signature is over the canonical JSON of the statement (sorted keys, no spaces). Verifying
needs only the public key, so an auditor can check a checkpoint without trusting CCP.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ccp.config import Settings


def canonical(statement: dict[str, Any]) -> bytes:
    return json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")


class Signer(Protocol):
    key_id: str

    def sign(self, message: bytes) -> bytes: ...

    def verify(self, message: bytes, signature: bytes) -> bool: ...


class LocalSigner:
    def __init__(self, path: Path) -> None:
        if path.is_file():
            loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
            if not isinstance(loaded, Ed25519PrivateKey):
                raise ValueError(f"{path} does not hold an Ed25519 key")
            self._key = loaded
        else:
            self._key = Ed25519PrivateKey.generate()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(
                self._key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
        public = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.key_id = "ed25519:" + hashlib.sha256(public).hexdigest()[:16]

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            self._key.public_key().verify(signature, message)
        except InvalidSignature:
            return False
        return True


class KmsSigner:
    def __init__(self, key_id: str, client: Any) -> None:
        self.key_id = key_id
        self._client = client

    def sign(self, message: bytes) -> bytes:
        answer = self._client.sign(
            KeyId=self.key_id,
            Message=message,
            MessageType="RAW",
            SigningAlgorithm="ECDSA_SHA_256",
        )
        signature: bytes = answer["Signature"]
        return signature

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            answer = self._client.verify(
                KeyId=self.key_id,
                Message=message,
                MessageType="RAW",
                Signature=signature,
                SigningAlgorithm="ECDSA_SHA_256",
            )
        except self._client.exceptions.KMSInvalidSignatureException:
            return False
        return bool(answer["SignatureValid"])


def signer_for(settings: Settings) -> LocalSigner | KmsSigner:
    if settings.ccp_audit_kms_key_id:
        import boto3

        client = boto3.client("kms", region_name=settings.aws_region)
        return KmsSigner(settings.ccp_audit_kms_key_id, client)
    return LocalSigner(settings.ccp_data_dir / "audit" / "signing-key.pem")


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))
