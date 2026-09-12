"""Tiny YAML front matter splitter for Markdown and text documents."""

from __future__ import annotations

from typing import Any

import yaml


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Return (front_matter, body). A document without front matter yields ({}, text)."""
    normalized = text.replace("\r\n", "\n")
    if not normalized.startswith("---\n"):
        return {}, normalized
    end = normalized.find("\n---\n", 4)
    if end == -1:
        return {}, normalized
    raw = yaml.safe_load(normalized[4:end]) or {}
    if not isinstance(raw, dict):
        raise ValueError("front matter must be a mapping")
    return dict(raw), normalized[end + 5 :]
