from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

Origin = Literal["live", "index", "snippet"]


@dataclass(slots=True)
class Page:
    """Clean page text. Passage offsets point into ``text``, and ``text`` is what
    the API returns as ``raw_content``, so ``raw_content[start:end] == content``."""

    url: str
    title: str
    text: str
    source: str
    published: datetime | None = None
    updated: datetime | None = None
    # live: fetched for this request; index: from the local index; snippet: the
    # page couldn't be fetched, so the provider snippet stands in for it.
    origin: Origin = "live"
    sources: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Passage:
    page: Page
    text: str
    start: int
    end: int
    heading: str | None = None
    embedding: list[float] | None = None
