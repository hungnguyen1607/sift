"""A polite crawler for building seed packs.

Reuses the live ``Fetcher`` (robots.txt on every hop, honest User-Agent, size
cap, failure cache), adds a per-host rate limit and internal-link BFS, and
cleans each page with the live pipeline's ``clean_html`` so the pack's offsets
and text match what the API returns.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlsplit

from sift.pipeline.clean import clean_html
from sift.pipeline.fetch import Fetcher, FetchError
from sift.pipeline.models import Page
from sift.providers.base import parse_ts

log = logging.getLogger(__name__)

_HTML_TYPES = ("text/html", "application/xhtml+xml")


def extract_links(html: str, base_url: str) -> list[str]:
    """Absolute URLs of ``<a href>`` links on a page."""
    try:
        from lxml import html as lxml_html

        doc = lxml_html.fromstring(html)
    except Exception:
        return []
    out = []
    for _, _, href, _ in doc.iterlinks():
        if href and not href.startswith(("mailto:", "javascript:", "#")):
            out.append(urljoin(base_url, href))
    return out


def same_origin(url: str, scope: str) -> bool:
    """True if ``url`` is on the same host and under the given path scope."""
    u, s = urlsplit(url), urlsplit(scope)
    if (u.hostname or "").lower() != (s.hostname or "").lower():
        return False
    return u.path.startswith(s.path.rstrip("/"))


class Crawler:
    def __init__(
        self,
        fetcher: Fetcher,
        *,
        delay: float = 1.0,
        max_pages: int = 2000,
        clean_in_thread: bool = True,
    ):
        self.fetcher = fetcher
        self.delay = delay
        self.max_pages = max_pages
        self.clean_in_thread = clean_in_thread
        self._last: dict[str, float] = {}

    async def _throttle(self, host: str) -> None:
        now = time.monotonic()
        wait = self._last.get(host, 0) + self.delay - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last[host] = time.monotonic()

    async def _clean(self, html: str, url: str) -> Page | None:
        if self.clean_in_thread:
            data = await asyncio.to_thread(clean_html, html, url)
        else:
            data = clean_html(html, url)
        if data is None or not data["text"].strip():
            return None
        return Page(
            url=url,
            title=data["title"] or url,
            text=data["text"],
            source="seed",
            published=parse_ts(data["published"]),
            updated=parse_ts(data["updated"]),
        )

    async def crawl(self, seeds: list[str], *, scope: str | None = None) -> AsyncIterator[Page]:
        """Breadth-first crawl from ``seeds``, staying under ``scope`` (a URL
        prefix). Yields cleaned pages; a page that can't be fetched or cleaned
        is skipped. One request per ``delay`` seconds per host."""
        queue: deque[str] = deque(seeds)
        seen: set[str] = set()
        yielded = 0
        scope = scope or urlsplit(seeds[0]).scheme + "://" + urlsplit(seeds[0]).netloc

        while queue and yielded < self.max_pages:
            url = queue.popleft()
            if url in seen:
                continue
            seen.add(url)
            host = (urlsplit(url).hostname or "").lower()
            await self._throttle(host)
            try:
                res = await self.fetcher.fetch(url)
            except FetchError as e:
                log.info("crawl skip %s: %s", url, e)
                continue
            if res.is_html:
                page = await self._clean(res.body, res.url)
            else:
                continue
            if page is not None:
                yield page
                yielded += 1
                for link in extract_links(res.body, res.url):
                    if link not in seen and same_origin(link, scope):
                        queue.append(link)


async def fetch_sitemap_urls(client, sitemap_url: str) -> list[str]:
    """Fetch a sitemap (or sitemap index) and return the page URLs inside it."""
    import xml.etree.ElementTree as ET

    resp = await client.get(sitemap_url)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    # sitemap index → nested sitemaps; urlset → pages
    ns = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    tag = root.tag
    if tag == ns + "sitemapindex":
        out: list[str] = []
        for sitemap in root.findall(ns + "sitemap"):
            loc = sitemap.findtext(ns + "loc")
            if loc:
                out.extend(await fetch_sitemap_urls(client, loc))
        return out
    return [u.findtext(ns + "loc") for u in root.findall(ns + "url") if u.findtext(ns + "loc")]
