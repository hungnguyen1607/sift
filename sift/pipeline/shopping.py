"""Reading real prices for the shopping vertical.

List results normally come from snippets alone, but a price is worth a fetch:
for the top store pages that allow crawling, read schema.org Product data (or a
Shopify feed), and expand a category page into the products it lists. Bounded by
a budget, with an early stop once enough products carry a price.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sift.pipeline.candidates import MergedCandidate, normalize_url
from sift.pipeline.structured import (
    SHOPIFY_TYPES,
    job_fields,
    product_fields,
    product_list,
    shopify_collection,
    shopify_collection_url,
    shopify_fields,
    shopify_json_url,
)
from sift.pipeline.verticals import Vertical, product_url, store_like
from sift.providers.base import Candidate

if TYPE_CHECKING:
    from sift.api.deps import Engine

# Shopping stops fetching once this many products carry a price: the last few
# pages are usually the slowest (large, or rate-limited, stores).
_ENOUGH_PRICED = 6
# One listing page can publish many products; count at most this many toward
# the early stop, so a single big category page can't satisfy it alone.
_PER_SITE_PRICE_CAP = 3


async def fetch_structured(
    engine: Engine,
    v: Vertical,
    todo: list[MergedCandidate],
    budget_s: float,
    enough_priced: int = _ENOUGH_PRICED,
) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """Fetch pages (robots.txt checked by the fetcher) and read their JSON-LD.
    Returns fields per page, and for shopping the products each listing page
    publishes. Whatever doesn't finish within the budget keeps its snippet only."""
    if engine.fetcher is None or not todo:
        return {}, {}
    reader = job_fields if v.name == "jobs" else product_fields
    shopping = v.name == "shopping"

    async def one(url: str) -> tuple[str, dict, list[dict]]:
        key = normalize_url(url)
        js = shopify_json_url(url) if shopping else None
        if js is not None:
            try:  # robots.txt is checked for the JSON URL too
                res = await engine.fetcher.fetch(js, accept=SHOPIFY_TYPES)
                fields = shopify_fields(res.body)
                if fields:
                    return key, fields, []
            except Exception:
                pass  # not a Shopify store: read the page itself
        feed = shopify_collection_url(url) if shopping else None
        if feed is not None:
            try:
                res = await engine.fetcher.fetch(feed, accept=SHOPIFY_TYPES)
                items = shopify_collection(res.body, url)
                if items:
                    return key, {}, items
            except Exception:
                pass
        res = await engine.fetcher.fetch(url)
        if not res.is_html:
            return key, {}, []
        items = product_list(res.body, url) if shopping else []
        return key, ({} if items else reader(res.body)), items

    tasks = [asyncio.create_task(one(m.candidate.url)) for m in todo]
    enough = enough_priced if shopping else None
    deadline = time.perf_counter() + budget_s
    done: set[asyncio.Task] = set()
    pending = set(tasks)
    while pending:
        left = deadline - time.perf_counter()
        if left <= 0:
            break
        finished, pending = await asyncio.wait(
            pending, timeout=left, return_when=asyncio.FIRST_COMPLETED
        )
        done |= finished
        if enough is not None and _priced(done) >= enough:
            break
    for t in pending:
        t.cancel()
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out: dict[str, dict] = {}
    listed: dict[str, list[dict]] = {}
    for t in done:
        if t.exception() is None:
            key, fields, items = t.result()
            if fields:
                if shopping:
                    fields["price_checked_at"] = now
                out[key] = fields
            if items:
                for it in items:
                    if "price" in it:
                        it["price_checked_at"] = now
                listed[key] = items
    return out, listed


def _priced(done: set[asyncio.Task]) -> int:
    n = 0
    for t in done:
        if not t.cancelled() and t.exception() is None:
            _, fields, items = t.result()
            n += "price" in fields
            n += min(sum("price" in it for it in items), _PER_SITE_PRICE_CAP)
    return n


def echoes_query(price: object, query: str) -> bool:
    """A snippet amount the query itself states ("under $100") isn't a price."""
    if not isinstance(price, float):
        return False
    return any(float(a.replace(",", "")) == price for a in re.findall(r"\d[\d,]*(?:\.\d+)?", query))


def shopping_todo(todo: list[MergedCandidate], n: int) -> list[MergedCandidate]:
    """Product pages first (they carry one price), then up to three store
    listing pages (each can carry many). Stores with nothing for crawlers and
    pages that sell nothing aren't fetched."""
    todo = [
        m for m in todo if not _no_product_data(m.candidate.url) and store_like(m.candidate.url)
    ]
    products = [m for m in todo if product_url(m.candidate.url)]
    others = [m for m in todo if not product_url(m.candidate.url)]
    picked = products[: max(n - 3, 0)] + others[:3]
    rest = [m for m in todo if m not in picked]
    return (picked + rest)[:n]


def expand_listings(
    merged: list[MergedCandidate],
    listed: dict[str, list[dict]],
    structured: dict[str, dict],
    seen: set[str],
    v: Vertical,
) -> list[MergedCandidate]:
    """Put the products a listing page publishes right after that page, as
    entries of their own, with the listing as ``fields.listed_on``."""
    out: list[MergedCandidate] = []
    for m in merged:
        out.append(m)
        for item in listed.get(normalize_url(m.candidate.url), []):
            url = v.canonical(item["url"]) if v.canonical else item["url"]
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            fields = {k: val for k, val in item.items() if k != "url"}
            fields["listed_on"] = m.candidate.url
            structured[key] = fields
            price = fields.get("price")
            snippet = fields["name"] + (
                f" — {price:g} {fields.get('currency', '')}".rstrip() if price else ""
            )
            c = Candidate(url=url, title=fields["name"], snippet=snippet, source=m.candidate.source)
            out.append(MergedCandidate(c, m.score, list(m.sources)))
    return out


# Stores whose pages carry no schema.org product data for crawlers (bot walls,
# client-rendered prices): fetching them only spends the fetch budget.
_NO_PRODUCT_DATA = ("amazon.", "ebay.", "walmart.", "aliexpress.", "temu.")


def _no_product_data(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return any(h in host for h in _NO_PRODUCT_DATA)
