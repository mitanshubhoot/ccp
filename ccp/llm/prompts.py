"""Versioned prompt files: `ccp/llm/prompts/<version>.md`, git-tracked, never edited in
place (a change is a new version). Each load returns the text and its sha256 so a verdict
and a step can name exactly what was sent."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    version: str
    text: str
    sha256: str


@lru_cache(maxsize=32)
def load_prompt(version: str) -> Prompt:
    path = PROMPTS_DIR / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"no prompt file for version {version!r} at {path}")
    text = path.read_text(encoding="utf-8")
    return Prompt(version=version, text=text, sha256=hashlib.sha256(text.encode()).hexdigest())


def prompt_files() -> list[Path]:
    return sorted(PROMPTS_DIR.glob("*.md"))
