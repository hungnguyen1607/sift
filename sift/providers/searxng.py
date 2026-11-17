import time

import httpx

from .base import Candidate, Provider, ProviderError, parse_ts, strip_html

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
    """

    name = "searxng"
    category_timeout = 5.0

    def __init__(self, client: httpx.AsyncClient, url: str, categories: str = "general"):
        self.client = client
        self.url = url.rstrip("/")
        self.categories = categories

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
    ) -> list[Candidate]:
        params = {"q": query, "format": "json", "categories": categories}
        if page > 1:
            params["pageno"] = page
        if time_range and time_range in _TIME_RANGE:
            params["time_range"] = _TIME_RANGE[time_range]
        # Vertical categories fan out to slower engines (video sites, news), so
        # they get longer than the shared client's provider timeout.
        timeout = http_timeout
        if timeout is None and categories != "general":
            timeout = self.category_timeout
        kwargs = {"timeout": timeout} if timeout else {}
        data = await self._get_json(self.client, f"{self.url}/search", params=params, **kwargs)
        out = []
        for r in data.get("results", []):
            url = r.get("url")
            if not url:
                continue
            out.append(
                Candidate(
                    url=url,
                    title=strip_html(r.get("title")) or url,
                    snippet=strip_html(r.get("content"), max_len=400),
                    source=self.name,
                    published=parse_ts(r.get("publishedDate")),
                    extra={k: r[k] for k in _EXTRA_KEYS if r.get(k) not in (None, "", [])},
                )
            )
            if len(out) >= limit:
                break
        if not out and data.get("unresponsive_engines"):
            down = ", ".join(f"{e} ({why})" for e, why in data["unresponsive_engines"][:4])
            raise ProviderError(f"searxng: no results, engines unavailable: {down}")
        return out

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
