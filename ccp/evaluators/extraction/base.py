"""The citation every extracted field carries."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Cited(BaseModel):
    """chunk_id from the fence's opening marker; quote copied verbatim from that chunk."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    quote: str
