"""Target validation lives in ccp.evaluators.targets so the agent layer can share it; this
module keeps the Layer 10 import path."""

from __future__ import annotations

from ccp.evaluators.targets import Resolution, resolve

__all__ = ["Resolution", "resolve"]
