"""Freshness: `time_range`/`fresh_only` filtering, and the per-provider recency
parameter mappings.

The date used for a passage is `max(published, updated)`. `fresh_only` drops
undated passages; `time_range` alone only demotes them (score × 0.5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sift.pipeline.rank import Ranked

TIME_RANGES = {
    "day": 1,
    "d": 1,
    "week": 7,
    "w": 7,
    "month": 30,
    "m": 30,
    "year": 365,
    "y": 365,
}

# Canonical short letter for the "qdr" / freshness parameter forms.
_LETTER = {
    "day": "d",
    "d": "d",
    "week": "w",
    "w": "w",
    "month": "m",
    "m": "m",
    "year": "y",
    "y": "y",
}


def window_days(time_range: str | None, fresh_only: bool) -> int | None:
    """The freshness window in days, or None for no window. `fresh_only` with no
    range means one year."""
    if time_range:
        return TIME_RANGES[time_range]
    if fresh_only:
        return 365
    return None


def cutoff_date(
    time_range: str | None, fresh_only: bool, now: datetime | None = None
) -> datetime | None:
    days = window_days(time_range, fresh_only)
    if days is None:
        return None
    return (now or datetime.now(UTC)) - timedelta(days=days)


def apply_freshness(
    ranked: list[Ranked], time_range: str | None, fresh_only: bool, now: datetime | None = None
) -> list[Ranked]:
    """Drop too-old passages, demote undated ones when only `time_range` is set,
    and re-sort. Returns the (possibly unchanged) list."""
    cutoff = cutoff_date(time_range, fresh_only, now)
    if cutoff is None:
        return ranked
    out: list[Ranked] = []
    for r in ranked:
        page = r.passage.page
        latest = page.updated or page.published
        if latest is not None:
            if latest.tzinfo is None:  # defensive: compare aware-vs-aware
                latest = latest.replace(tzinfo=UTC)
            if latest < cutoff:
                continue
        elif fresh_only:
            continue  # can't prove freshness → drop
        else:
            r.score *= 0.5  # undated → rank lower
        out.append(r)
    out.sort(key=lambda r: -r.score)
    return out


def cutoff_epoch(
    time_range: str | None, fresh_only: bool, now: datetime | None = None
) -> int | None:
    """Unix seconds for providers that take a `fromdate`/`numericFilters` cutoff."""
    cutoff = cutoff_date(time_range, fresh_only, now)
    return int(cutoff.timestamp()) if cutoff else None


def brave_freshness(time_range: str | None) -> str | None:
    """Brave `freshness=pd|pw|pm|py`."""
    if not time_range:
        return None
    return "p" + _LETTER[time_range]


def serper_tbs(time_range: str | None) -> str | None:
    """Serper `tbs=qdr:d|w|m|y`."""
    if not time_range:
        return None
    return "qdr:" + _LETTER[time_range]


def tavily_time_range(time_range: str | None) -> str | None:
    """Tavily `time_range=day|week|month|year` (full words)."""
    if not time_range:
        return None
    return {
        "d": "day",
        "day": "day",
        "w": "week",
        "week": "week",
        "m": "month",
        "month": "month",
        "y": "year",
        "year": "year",
    }[time_range]
