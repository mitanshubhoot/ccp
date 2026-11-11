"""The catalogue's version (spec.md A-128): one hash over everything that says what is
tested and why. Frameworks, controls, tests, the impact table and the declarative checks
all live in this directory and ship with the code; a change to any of them is a new
version, and every verdict's manifest and every state row records the version in force.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

from ccp.knowledge.graph import KNOWLEDGE_DIR


def files(directory: Path = KNOWLEDGE_DIR) -> list[Path]:
    """The catalogue's files, in a fixed order."""
    found = [
        *sorted((directory / "frameworks").glob("*.yaml")),
        directory / "controls.yaml",
        directory / "tests.yaml",
        directory / "impact.yaml",
        *sorted((directory / "checks").glob("*.yaml")),
    ]
    return [path for path in found if path.is_file()]


@lru_cache(maxsize=4)
def version(directory: Path = KNOWLEDGE_DIR) -> str:
    digest = hashlib.sha256()
    for path in files(directory):
        digest.update(path.relative_to(directory).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:12]
