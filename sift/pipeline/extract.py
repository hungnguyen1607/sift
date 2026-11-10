"""`/v1/extract`: fetch the clean text of a set of URLs, in Tavily's shape.

Source order per URL: the local index (if fresh), the URL → page cache, a
provider that owns the URL, then a live fetch + clean. Failures are collected
per URL with a reason instead of failing the whole request.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from sift.pipeline.candidates import normalize_url
from sift.pipeline.fetch import FetchError
from sift.pipeline.models import Page
from sift.providers.base import parse_ts

if TYPE_CHECKING:
    from sift.api.deps import Engine

log = logging.getLogger(__name__)

DEFAULT_BUDGET = 5.0
MAX_URLS = 20


async def extract_urls(
    engine: Engine, urls: list[str], budget_s: float = DEFAULT_BUDGET
) -> tuple[list[dict], list[dict]]:
    # Dedup by normalized URL but keep the original for fetching.
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for u in urls[:MAX_URLS]:
        norm = normalize_url(u)
        if norm in seen:
            continue
        seen.add(norm)
        pairs.append((norm, u))

    results: list[dict] = []
    to_get: list[tuple[str, str]] = []

    indexed = await engine.index.extractable([n for n, _ in pairs]) if engine.index else {}
    for norm, orig in pairs:
        if norm in indexed:
            results.append(indexed[norm])
            continue
        cached = await engine.page_cache.get(norm) if engine.page_cache else None
        if cached:
            results.append(cached)
            continue
        to_get.append((norm, orig))

    failed: list[dict] = []
    if to_get:
        tasks = {
            asyncio.create_task(_extract_one(engine, orig)): (norm, orig) for norm, orig in to_get
        }
        done, pending = await asyncio.wait(tasks, timeout=budget_s)
        for t in pending:
            t.cancel()
            failed.append({"url": tasks[t][1], "error": "timeout"})
        for t in done:
            norm, orig = tasks[t]
            if t.exception() is not None:
                log.info("extract %s: %s", orig, t.exception())
                failed.append({"url": orig, "error": str(t.exception()) or "error"})
                continue
            page = t.result()
            if page is None:
                failed.append({"url": orig, "error": "no content"})
                continue
            results.append(
                {
                    "url": page.url,
                    "title": page.title,
                    "raw_content": page.text,
                    "source": page.source,
                    "published": page.published,
                    "updated": page.updated,
                }
            )
            if engine.page_cache is not None:
                await engine.page_cache.set(
                    norm,
                    {
                        "url": page.url,
                        "title": page.title,
                        "raw_content": page.text,
                        "source": page.source,
                        "published": page.published.isoformat() if page.published else None,
                        "updated": page.updated.isoformat() if page.updated else None,
                    },
                )
            engine.schedule_index([page])
    return results, failed


async def _extract_one(engine: Engine, url: str) -> Page | None:
    owner = next((p for p in engine.providers if p.owns(url)), None)
    if owner is not None:
        if not await engine.quota.try_acquire(owner.name):
            raise RuntimeError(f"{owner.name}: quota exhausted")
        page = await owner.fetch_page(url)
        if page is not None:
            return page
        raise RuntimeError(f"{owner.name}: no content")
    try:
        res = await engine.fetcher.fetch(url)
    except FetchError as e:
        raise RuntimeError(str(e)) from e
    from sift.pipeline.clean import MAX_TEXT_CHARS
    from sift.pipeline.search import _clean

    if res.is_html:
        cleaned = await _clean(engine.clean_executor, res.body, res.url)
        if cleaned is None:
            raise RuntimeError("no extractable content")
        title = cleaned["title"] or url
        published = parse_ts(cleaned["published"])
        updated = parse_ts(cleaned["updated"])
        text = cleaned["text"]
    else:
        from sift.pipeline.clean import tidy_markdown

        text = tidy_markdown(res.body[:MAX_TEXT_CHARS])
        title, published, updated = url, None, res.last_modified
    if not text.strip():
        raise RuntimeError("empty page")
    return Page(res.url, title, text, "extract", published, updated)
