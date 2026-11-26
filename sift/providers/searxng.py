import asyncio
import time

import httpx

from .base import Candidate, Provider, ProviderError, parse_ts, strip_html
from .engine_health import EngineHealth

# Sift's time_range values → SearXNG's.
_TIME_RANGE = {
    "d": "day",
    "day": "day",
    "w": "week",
    "week": "week",
    "m": "month",
    "month": "month",
    "y": "year",
    "year": "year",
}
# Result keys copied into Candidate.extra for the vertical parsers (videos carry
# `length`/`author`, news `metadata`/`pubdate`, map results `address`/`latitude`/…).
_EXTRA_KEYS = (
    "engine",
    "score",
    "category",
    "length",
    "author",
    "metadata",
    "pubdate",
    "thumbnail",
    "iframe_src",
    "address",
    "latitude",
    "longitude",
    "type",
    "data",
    "links",
    "views",
)


class SearXNGProvider(Provider):
    """Self-hosted SearXNG meta-search (federates Google, Bing, DuckDuckGo, …).

    Sift queries SearXNG's stable JSON API and turns its raw links into
    candidates that the pipeline then fetches, cleans and ranks. The HTML
    parsing lives in SearXNG, not Sift, so keeping the SearXNG container up to
    date is all the "engine changed" maintenance this needs. Self-hosted, so
    uncapped (Sift relies on SearXNG's own rate limiter).

    Besides general web search, ``search_category`` reaches SearXNG's vertical
    categories (videos, news, map, social media, science, it) and
    ``search_pages`` pages through results for listing searches.

    Engines that report a rate limit or CAPTCHA are skipped until their
    cooldown ends (``EngineHealth``), and requests are paced (at most
    ``max_in_flight`` at once, ``min_gap_s`` apart) so a burst of searches
    doesn't trip the engines' limits in the first place.
    """

    name = "searxng"
    category_timeout = 5.0
    # Categories whose engines are slow by nature (OSM/Photon geocoding) and
    # would come back empty under the normal engine cutoff.
    slow_categories = {"map": 3.0}
    _SNIPPET_MAX = 400  # chars of result content kept as the candidate snippet
    _GRACE_S = 1.0  # extra HTTP time for SearXNG to answer after its engine cutoff
    _MAX_REPORTED_DOWN = 4  # unresponsive engines named in an error message

    def __init__(
        self,
        client: httpx.AsyncClient,
        url: str,
        categories: str = "general",
        *,
        health: EngineHealth | None = None,
        max_in_flight: int = 4,
        min_gap_s: float = 0.15,
        engine_timeout: float | None = 2.0,
        category_engine_timeout: float | None = 1.5,
    ):
        self.client = client
        self.url = url.rstrip("/")
        self.categories = categories
        self.health = health or EngineHealth()
        # SearXNG's `timeout_limit`: answer with whatever engines replied by
        # then, instead of waiting ~3 s for one slow engine (Vimeo, Wikidata).
        self.engine_timeout = engine_timeout
        self.category_engine_timeout = category_engine_timeout
        self.min_gap_s = min_gap_s
        self._slots = asyncio.Semaphore(max_in_flight)
        self._gap_lock = asyncio.Lock()
        self._last_start = 0.0

    async def _paced(self, coro_fn):
        async with self._slots:
            async with self._gap_lock:
                wait = self._last_start + self.min_gap_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last_start = time.monotonic()
            return await coro_fn()

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        return await self.search_category(query, self.categories, limit, time_range=time_range)

    async def search_category(
        self,
        query: str,
        categories: str,
        limit: int,
        *,
        page: int = 1,
        time_range: str | None = None,
        http_timeout: float | None = None,
        engine_timeout: float | None = None,
    ) -> list[Candidate]:
        params = {"q": query, "format": "json", "categories": categories}
        if page > 1:
            params["pageno"] = page
        if time_range and time_range in _TIME_RANGE:
            params["time_range"] = _TIME_RANGE[time_range]
        engine_cutoff_s = self._engine_cutoff(categories, engine_timeout)
        if engine_cutoff_s:
            params["timeout_limit"] = engine_cutoff_s
        skip = self.health.disabled_param(categories)
        if skip:
            params["disabled_engines"] = skip
        # Vertical categories fan out to slower engines (video sites, news), so
        # they get longer than the shared client's provider timeout.
        timeout = http_timeout
        if timeout is None and categories != "general":
            timeout = self.category_timeout
        if engine_cutoff_s:  # leave SearXNG time to answer after its engines' cutoff
            timeout = max(timeout or 0.0, engine_cutoff_s + self._GRACE_S)
        kwargs = {"timeout": timeout} if timeout else {}
        data = await self._paced(
            lambda: self._get_json(self.client, f"{self.url}/search", params=params, **kwargs)
        )
        self.health.report(data.get("unresponsive_engines") or [])
        out: list[Candidate] = []
        for r in data.get("results", []):
            c = self._to_candidate(r)
            if c is not None:
                out.append(c)
            if len(out) >= limit:
                break
        if not out and (data.get("unresponsive_engines") or skip):
            raise ProviderError(self._unavailable_message(data))
        return out

    def _engine_cutoff(self, categories: str, engine_timeout: float | None) -> float | None:
        """SearXNG's ``timeout_limit``: answer with whatever engines replied by
        then. A caller's ``engine_timeout`` wins (a secondary query that mustn't
        hold the rest up); else it's the per-category default, floored for slow
        categories (geocoders) that would otherwise come back empty."""
        if engine_timeout is not None:
            return engine_timeout
        cutoff = self.engine_timeout if categories == "general" else self.category_engine_timeout
        if cutoff and categories in self.slow_categories:
            cutoff = max(cutoff, self.slow_categories[categories])
        return cutoff

    def _to_candidate(self, r: dict) -> Candidate | None:
        url = r.get("url")
        if not url:
            return None
        return Candidate(
            url=url,
            title=strip_html(r.get("title")) or url,
            snippet=strip_html(r.get("content"), max_len=self._SNIPPET_MAX),
            source=self.name,
            published=parse_ts(r.get("publishedDate")),
            extra={k: r[k] for k in _EXTRA_KEYS if r.get(k) not in (None, "", [])},
        )

    def _unavailable_message(self, data: dict) -> str:
        unresponsive = (data.get("unresponsive_engines") or [])[: self._MAX_REPORTED_DOWN]
        down = [f"{e} ({why})" for e, why in unresponsive]
        # `e not in str(down)`: a cheap substring check so an engine that is both
        # unresponsive now and already cooling down isn't listed twice.
        down += [f"{e} (cooling down)" for e in self.health.cooling() if e not in str(down)]
        return f"searxng: no results, engines unavailable: {', '.join(down)}"

    async def search_pages(
        self,
        query: str,
        categories: str,
        want: int,
        *,
        max_pages: int = 5,
        time_range: str | None = None,
        budget_s: float | None = None,
        keep=None,
    ) -> list[Candidate]:
        """Page through results until ``want`` distinct URLs (that pass
        ``keep``, if given), an empty or all-repeat page, or ``max_pages``.
        Pages are fetched one at a time so SearXNG's limiter (and the engines
        behind it) aren't hammered."""
        seen: set[str] = set()
        out: list[Candidate] = []
        t0 = time.monotonic()
        for page in range(1, max_pages + 1):
            # Don't start a page that likely can't finish within the budget.
            if page > 1 and budget_s is not None and time.monotonic() - t0 > budget_s / 2:
                break
            try:
                batch = await self.search_category(
                    query,
                    categories,
                    100,
                    page=page,
                    time_range=time_range,
                    http_timeout=self.category_timeout,
                )
            except Exception:
                if out:  # keep what earlier pages found
                    break
                raise
            new = [c for c in batch if c.url not in seen]
            for c in new:
                seen.add(c.url)
            out.extend(new)
            kept = sum(1 for c in out if keep(c.url)) if keep else len(out)
            if not new or kept >= want:
                break
        return out
