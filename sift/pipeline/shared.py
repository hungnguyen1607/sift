"""Small types and helpers shared across the search pipeline modules.

Kept in their own module so ``search`` (the orchestrator) and the stages it
calls (``listing``, ``materialize``) can all import them without importing each
other — that would be a cycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sift.pipeline.rank import Ranked

if TYPE_CHECKING:
    from sift.api.deps import Engine


@dataclass(slots=True)
class SearchOutcome:
    results: list[Ranked]
    providers_used: list[str]
    providers_skipped: dict[str, str]
    cache_hit: bool
    timings_ms: dict[str, int] = field(default_factory=dict)
    page_texts: dict[str, str] = field(default_factory=dict)  # url_norm → raw_content
    tokens_used: int | None = None
    sub_queries: list[str] | None = None
    answer: str | None = None
    vertical: str = "web"
    # When auto-detection picked a list vertical that returned nothing, the
    # query is retried as a plain web search: `vertical` then reads "web",
    # `detected_vertical` keeps the original guess, and this flag is set.
    detected_vertical: str | None = None
    fell_back_to_web: bool = False


def _ms(t0: float) -> int:
    """Milliseconds elapsed since ``t0`` (a ``time.perf_counter()`` reading)."""
    return round((time.perf_counter() - t0) * 1000)


def _searxng(engine: Engine):
    """The SearXNG provider among the engine's fallbacks, or None."""
    from sift.providers.searxng import SearXNGProvider

    return next((p for p in engine.fallbacks if isinstance(p, SearXNGProvider)), None)
