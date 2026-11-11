"""Cells: where a tenant's data and model calls live (spec.md A-172).

A cell is one deployment of the same code and migrations with its own database, buckets and
audit trail (config/cells.yaml). A tenant lives in one cell and a process serves one cell,
so a tenant from another cell is refused at the one place every tenant is loaded
(`TenantContext.for_id`). A cell also says how models are reached: the first-party API, or
Bedrock inference profiles in the cell's region, with CCP's pinned model ids mapped to the
provider's; and which embeddings endpoint it uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml

from ccp.config import REPO_ROOT, Settings

CELLS_FILE = REPO_ROOT / "config" / "cells.yaml"


@dataclass(frozen=True)
class Cell:
    cell_id: str
    region: str
    residency: Literal["us", "eu"]
    provider: Literal["anthropic", "bedrock"]
    model_region: str | None
    profiles: dict[str, str] = field(default_factory=dict)
    voyage_url: str = "https://api.voyageai.com/v1/embeddings"

    def model_id(self, pinned: str) -> str:
        """The provider's id for a pinned model; a Bedrock cell refuses a model it has no
        profile for rather than calling one outside its region."""
        if self.provider == "anthropic":
            return pinned
        if pinned not in self.profiles:
            raise ValueError(f"cell {self.cell_id} has no {self.provider} profile for {pinned}")
        return self.profiles[pinned]


@lru_cache(maxsize=2)
def load(path: Path = CELLS_FILE) -> dict[str, Cell]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: dict[str, Cell] = {}
    for cell_id, body in (raw.get("cells") or {}).items():
        models: dict[str, Any] = dict(body.get("models") or {})
        out[str(cell_id)] = Cell(
            str(cell_id),
            str(body["region"]),
            body["residency"],
            models.get("provider", "anthropic"),
            models.get("region"),
            {str(k): str(v) for k, v in (models.get("profiles") or {}).items()},
            str(body.get("voyage_url") or "https://api.voyageai.com/v1/embeddings"),
        )
    return out


def current(settings: Settings) -> Cell:
    cells = load()
    if settings.ccp_cell not in cells:
        raise ValueError(f"CCP_CELL={settings.ccp_cell!r} is not in cells.yaml: {sorted(cells)}")
    return cells[settings.ccp_cell]


class ModelRouted:
    """The SDK client with every `model` argument mapped to the cell's provider id."""

    def __init__(self, client: Any, cell: Cell) -> None:
        self._client = client
        self._cell = cell
        self.messages = _Messages(client.messages, cell)


class _Messages:
    def __init__(self, messages: Any, cell: Cell) -> None:
        self._messages = messages
        self._cell = cell
        self.batches = messages.batches if hasattr(messages, "batches") else None

    def _mapped(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        if "model" in kwargs:
            kwargs = {**kwargs, "model": self._cell.model_id(str(kwargs["model"]))}
        return kwargs

    def create(self, **kwargs: Any) -> Any:
        return self._messages.create(**self._mapped(kwargs))

    def parse(self, **kwargs: Any) -> Any:
        return self._messages.parse(**self._mapped(kwargs))

    def stream(self, **kwargs: Any) -> Any:
        return self._messages.stream(**self._mapped(kwargs))
