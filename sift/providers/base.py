"""Provider interface and the quota manager that makes overspending impossible."""

from __future__ import annotations

import html
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

if TYPE_CHECKING:
    from sift.pipeline.models import Page

log = logging.getLogger(__name__)

Period = Literal["day", "month", "minute", "second", "lifetime"]

# Expiry in seconds for each period's counter key. Lifetime counters never
# expire; the tiny daily/monthly/minute keys are cleaned up automatically.
_PERIOD_TTL: dict[Period, int | None] = {
    "day": 2 * 86400,
    "month": 40 * 86400,
    "minute": 120,
    "second": 3,
    "lifetime": None,
}


@dataclass(slots=True)
class Candidate:
    """A candidate link returned by a provider, before fetching or ranking."""

    url: str
    title: str
    snippet: str
    source: str
    published: datetime | None = None
    updated: datetime | None = None
    # Full page text in Markdown when the API already supplied it (StackExchange
    # bodies), so the page never needs fetching.
    content: str | None = None
    extra: dict = field(default_factory=dict)


class ProviderError(Exception):
    """A provider failed (HTTP error, bad payload, rate limited upstream)."""


class Provider(ABC):
    """A source of candidate links.

    Subclasses set ``name`` and, for anything with a free-tier limit, ``cap`` and
    ``period``. The quota manager enforces the cap before ``search`` is called.
    """

    name: str
    cap: int | None = None
    period: Period = "month"
    # True for upstream keyword APIs that AND every term; they get a rewritten
    # query without filler words. The local index parses natural language itself.
    keyword_search: bool = True
    # Fine-grained rate limit (max calls per window in seconds), enforced by the
    # quota manager on top of the cap. E.g. Brave allows 1 request/s, arXiv 1/3s.
    rate_limit: tuple[int, int] | None = None
    # Only queried when the query looks academic (paper, study, theorem, ...).
    academic_only: bool = False

    @abstractmethod
    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]: ...

    def owns(self, url: str) -> bool:
        """True if this provider fetches ``url``'s content through its API
        (instead of Sift scraping the page)."""
        return False

    async def fetch_page(self, url: str) -> Page | None:
        return None

    async def _get_json(self, client: httpx.AsyncClient, url: str, **kwargs) -> dict:
        try:
            resp = await client.get(url, **kwargs)
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e
        if resp.status_code == 429:
            raise ProviderError(f"{self.name}: rate limited upstream")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(f"{self.name}: non-JSON response: {resp.text[:80]!r}") from e

    async def _post_json(self, client: httpx.AsyncClient, url: str, **kwargs) -> dict:
        try:
            resp = await client.post(url, **kwargs)
        except httpx.HTTPError as e:
            raise ProviderError(f"{self.name}: {type(e).__name__}: {e}") from e
        if resp.status_code == 429:
            raise ProviderError(f"{self.name}: rate limited upstream")
        if resp.status_code >= 400:
            raise ProviderError(f"{self.name}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderError(f"{self.name}: non-JSON response: {resp.text[:80]!r}") from e


class QuotaManager:
    """Per-provider usage counters in Redis, one key per provider per period.

    ``try_acquire`` reserves one call. INCR is atomic, so concurrent callers can
    never push the counter past the cap: anyone who lands above it gives the
    reservation back and is refused.

    If Redis is unreachable, capped providers are refused (fail closed, so no
    surprise spend) and uncapped providers are allowed (fail open).
    """

    def __init__(self, redis: Redis, caps: dict[str, tuple[int | None, Period]]):
        self.redis = redis
        self.caps = caps

    @staticmethod
    def period_key(period: Period, now: datetime | None = None) -> str:
        now = now or datetime.now(UTC)
        if period == "lifetime":
            return "lifetime"
        return now.strftime(
            "%Y-%m-%d:%H:%M:%S"
            if period == "second"
            else "%Y-%m-%d:%H:%M"
            if period == "minute"
            else "%Y-%m-%d"
            if period == "day"
            else "%Y-%m"
        )

    def _key(self, provider: str, period: Period, now: datetime | None = None) -> str:
        return f"sift:quota:{provider}:{self.period_key(period, now)}"

    async def try_acquire(self, provider: str, now: datetime | None = None) -> bool:
        cap, period = self.caps.get(provider, (None, "month"))
        key = self._key(provider, period, now)
        try:
            used = await self.redis.incr(key)
            if used == 1:
                ttl = _PERIOD_TTL[period]
                if ttl is not None:
                    await self.redis.expire(key, ttl)
            if cap is not None and used > cap:
                await self.redis.decr(key)
                return False
            return True
        except RedisError as e:
            log.warning("quota store unavailable (%s); %s", e, "denying" if cap else "allowing")
            return cap is None

    async def try_rate(self, provider: str, limit: int, window_s: int) -> bool:
        """Fine-grained fixed-window rate limit (best-effort: the cap above is
        the fail-closed guarantee; this only smooths bursts)."""
        key = f"sift:rate:{provider}"
        try:
            used = await self.redis.incr(key)
            if used == 1:
                await self.redis.expire(key, window_s)
            return used <= limit
        except RedisError:
            return True

    async def usage(self, now: datetime | None = None) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for provider, (cap, period) in self.caps.items():
            try:
                used = int(await self.redis.get(self._key(provider, period, now)) or 0)
            except RedisError:
                used = None
            out[provider] = {
                "used": used,
                "cap": cap,
                "period": period,
                "remaining": None if cap is None or used is None else max(cap - used, 0),
            }
        return out


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(text: str | None, max_len: int | None = None) -> str:
    """Strip tags and entities from API snippets ("<span class=searchmatch>...")."""
    if not text:
        return ""
    out = _WS_RE.sub(" ", html.unescape(_TAG_RE.sub(" ", text))).strip()
    if max_len and len(out) > max_len:
        cut = out[:max_len].rsplit(" ", 1)[0]
        out = cut + "…"
    return out


def parse_ts(value) -> datetime | None:
    """Parse an ISO-8601 string or a Unix timestamp into an aware datetime."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, int | float):
            return datetime.fromtimestamp(value, UTC)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
