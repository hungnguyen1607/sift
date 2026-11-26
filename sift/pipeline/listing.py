"""List verticals: many entries built from search-result titles and snippets.

People, videos, posts, jobs, products and places come back as a *list* of
entries with structured ``fields``, not fetched-and-quoted passages. Sites that
block crawlers are never fetched; jobs and shopping fetch the top crawlable
results once for their schema.org data (salary, price). ``run_vertical`` drives
it: gather → dedup → (optional structured fetch) → field assembly → rank.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sift.pipeline.candidates import (
    RRF_K,
    CandidateResult,
    MergedCandidate,
    gather_candidates,
    normalize_url,
    try_provider,
)
from sift.pipeline.fresh import apply_freshness
from sift.pipeline.materialize import _passages, _snippet_page
from sift.pipeline.models import Page
from sift.pipeline.pack import pack
from sift.pipeline.rank import Ranked, bm25_scores, select
from sift.pipeline.shared import SearchOutcome, _ms, _searxng
from sift.pipeline.shopping import echoes_query, expand_listings, fetch_structured, shopping_todo
from sift.pipeline.verticals import (
    Vertical,
    clean,
    coverage,
    drop_far_places,
    key_terms,
    named_sites,
    on_site,
    pick_sites,
    price_cap,
    price_query,
    product_url,
    requested_count,
    search_query,
    site_target,
    store_like,
)

if TYPE_CHECKING:
    from sift.api.deps import Engine

log = logging.getLogger(__name__)

# Ranking weights for list entries (added on top of the RRF source score).
_NAMED_SITE_BONUS = 1.0  # a platform the query named ("reddit posts about …")
_PRICE_BONUS = 0.5  # shopping: a product that carries a price
_PRODUCT_URL_BONUS = 0.25  # shopping: a product URL, not a category page
_NON_STORE_PENALTY = 0.5  # shopping: Wikipedia, tag pages, forums
# Dedupe entries by URL only: a threshold above 1 turns the text check off,
# because entries share boilerplate ("Experience: X · Education: Y").
_DEDUPE_URL_ONLY = 1.01


async def run_vertical(
    engine: Engine,
    query: str,
    v: Vertical,
    *,
    max_results: int,
    max_tokens: int | None,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    fresh_only: bool,
    time_range: str | None,
) -> SearchOutcome:
    """A list of entries (people, videos, posts, jobs, products, places) built
    from search-result titles and snippets. Sites that block crawlers are never
    fetched; for jobs and shopping the top results that allow crawling are
    fetched once, for their schema.org JSON-LD (salary, price, …)."""
    cfg = engine.settings
    timings: dict[str, int] = {}
    t0 = time.perf_counter()
    want = min(max_results, cfg.listing_max_results)
    q = search_query(v, query)
    terms = key_terms(v, query)

    cres, errors = await _gather_entries(
        engine, v, query, q, want, include_domains, exclude_domains, time_range, timings
    )
    timings["candidates"] = _ms(t0)

    merged, seen = _dedup_entries(cres.candidates, v)

    structured: dict[str, dict] = {}
    if v.fetch:
        t1 = time.perf_counter()
        merged, structured = await _structured_fetch(engine, v, merged, seen, cfg)
        timings["fetch"] = _ms(t1)

    pool_pages = _entry_pages(merged, structured, v, query)

    t2 = time.perf_counter()
    chosen, tokens_used = _rank_entries(
        pool_pages, v, query, terms, q, want, time_range, fresh_only, max_tokens
    )
    timings["rank"] = _ms(t2)

    skipped = {**cres.providers_skipped, **errors}
    if len(chosen) < want and requested_count(query):
        skipped["listing"] = f"found {len(chosen)} of {want}"
    return SearchOutcome(
        results=chosen,
        providers_used=list(cres.providers_used),
        providers_skipped=skipped,
        cache_hit=False,
        timings_ms=timings,
        tokens_used=tokens_used,
    )


async def _gather_entries(
    engine: Engine,
    v: Vertical,
    query: str,
    q: str,
    want: int,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    time_range: str | None,
    timings: dict[str, int],
) -> tuple[CandidateResult, dict[str, str]]:
    """Candidates from the vertical's own sources plus, for an explicit site:
    query or a vertical with no sources of its own, the normal web providers."""
    cfg = engine.settings
    site = site_target(q)
    domains = include_domains or ([site[0]] if site else None)
    # More result pages only when more entries are wanted: every page is another
    # round of requests to the engines behind SearXNG, which rate-limit hard.
    pages = max(1, min(cfg.listing_max_pages, -(-want // 10)))
    errors: dict[str, str] = {}

    extra = _vertical_candidates(
        engine,
        q,
        v,
        want,
        time_range,
        pages=pages,
        budget_s=cfg.listing_budget,
        errors=errors,
        raw_query=query,
        timings=timings,
    )
    if site is not None or not (v.categories or v.sites or v.providers):
        # An explicit site: query (or a vertical with no sources of its own):
        # the normal providers plus the web backend, restricted to that site.
        cres = await gather_candidates(
            q,
            engine.providers,
            engine.quota,
            per_provider=max(cfg.listing_candidates, want),
            budget_s=cfg.provider_timeout,
            include_domains=domains,
            exclude_domains=exclude_domains,
            fallbacks=engine.fallbacks,
            depth="advanced",  # fire the web backend up front: snippets are all we get
            time_range=time_range,
            search_web=cfg.search_web,
            extra=extra,
            extra_budget_s=cfg.listing_budget + 0.5,
        )
    else:
        cres = await gather_candidates(
            q,
            [],
            engine.quota,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            time_range=time_range,
            extra=extra,
            extra_budget_s=cfg.listing_budget + 0.5,
        )
    return cres, errors


def _dedup_entries(
    candidates: list[MergedCandidate], v: Vertical
) -> tuple[list[MergedCandidate], set[str]]:
    """Drop entries that aren't the kind asked for (a LinkedIn post in a people
    search), then keep one per canonical URL."""
    merged: list[MergedCandidate] = []
    seen: set[str] = set()
    for m in candidates:
        if v.name == "social" and m.candidate.extra.get("discussion_url"):
            m.candidate.url = m.candidate.extra["discussion_url"]  # the thread, not the article
        url = m.candidate.url
        if not urlsplit(url).netloc:  # a relative link some engine passed through
            continue
        if v.url_ok is not None and not v.url_ok(url):
            continue
        if v.canonical is not None:
            m.candidate.url = url = v.canonical(url)
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)
    return merged, seen


async def _structured_fetch(
    engine: Engine, v: Vertical, merged: list[MergedCandidate], seen: set[str], cfg
) -> tuple[list[MergedCandidate], dict[str, dict]]:
    """Fetch the top crawlable entries for schema.org fields, and expand any
    store listing page into the products it publishes."""
    todo = [m for m in merged if "job" not in m.candidate.extra]
    if v.name == "shopping":
        todo = shopping_todo(todo, v.fetch)
    todo = todo[: v.fetch]
    structured, listed = await fetch_structured(engine, v, todo, cfg.structured_fetch_budget)
    if listed:
        merged = expand_listings(merged, listed, structured, seen, v)
    return merged, structured


def _entry_pages(
    merged: list[MergedCandidate], structured: dict[str, dict], v: Vertical, query: str
) -> list[Page]:
    """Build one snippet ``Page`` per entry, with its ``fields`` assembled from
    the vertical's parser, provider extras, and any fetched structured data."""
    pool_pages: list[Page] = []
    for m in merged:
        c = m.candidate
        if not c.snippet:
            c.snippet = c.title
        page = _snippet_page(m)
        if page is None:
            continue
        page.title = _strip_site_suffix(page.title)
        fields = dict(v.parse(c)) if v.parse else {}
        fields.update(c.extra.get("job") or {})
        fields.update({k: val for k, val in (c.extra.get("person") or {}).items() if val})
        found = structured.get(normalize_url(c.url), {})
        if "price" in found:  # the page's own price beats one read from the snippet
            fields.pop("price_source", None)
        fields.update(found)
        if fields.get("price_source") == "snippet" and echoes_query(fields["price"], query):
            for k in ("price", "currency", "price_source"):
                fields.pop(k, None)
        page.fields = clean(fields) or None
        page.sources = list(m.sources)
        pool_pages.append(page)

    if v.name == "places":
        keep = drop_far_places([p.fields for p in pool_pages])
        pool_pages = [p for p, k in zip(pool_pages, keep, strict=True) if k]
    cap = price_cap(query) if v.name == "shopping" else None
    if cap is not None:
        pool_pages = [
            p
            for p in pool_pages
            if not isinstance((p.fields or {}).get("price"), float) or p.fields["price"] <= cap
        ]
    return pool_pages


def _rank_entries(
    pool_pages: list[Page],
    v: Vertical,
    query: str,
    terms: list[str],
    q: str,
    want: int,
    time_range: str | None,
    fresh_only: bool,
    max_tokens: int | None,
) -> tuple[list[Ranked], int | None]:
    """Rank entries: those with every key term first, then the sources' own
    order and keyword relevance fused as RRF, with per-vertical bonuses."""
    pool = _passages(pool_pages)
    hays = [f"{p.page.title} {p.text} {_fields_text(p.page.fields)}" for p in pool]
    lexical = bm25_scores(" ".join(terms) or q, hays)
    lex_rank = {
        i: r
        for r, i in enumerate(
            sorted((i for i, s in enumerate(lexical) if s > 0), key=lambda i: -lexical[i]), 1
        )
    }
    # A platform the query names ("reddit posts about …") comes before others.
    named = named_sites(v, query)
    ranked = []
    for i, p in enumerate(pool):
        rrf = 1 / (RRF_K + i + 1) + (1 / (RRF_K + lex_rank[i]) if i in lex_rank else 0.0)
        cov = 1.0 if v.rank_by_source else coverage(v, terms, hays[i])
        if named and any(on_site(p.page.url, s) for s in named):
            cov += _NAMED_SITE_BONUS
        if v.name == "shopping":  # a priced product beats a category page
            cov += _PRICE_BONUS * ("price" in (p.page.fields or {}))
            cov += _PRODUCT_URL_BONUS * product_url(p.page.url)
            cov -= _NON_STORE_PENALTY * (not store_like(p.page.url))
        if v.rank_by_source:
            rrf = 1 / (RRF_K + i + 1)
        ranked.append(Ranked(p, cov + rrf, lex_rank.get(i)))
    ranked.sort(key=lambda r: -r.score)
    ranked = apply_freshness(ranked, time_range, fresh_only)
    chosen = select(ranked, want, per_site=v.per_site or want, dup_threshold=_DEDUPE_URL_ONLY)
    return pack(chosen, max_tokens)


async def _vertical_candidates(
    engine: Engine,
    query: str,
    v: Vertical,
    want: int,
    time_range: str | None,
    *,
    pages: int,
    budget_s: float,
    errors: dict[str, str] | None = None,
    raw_query: str | None = None,
    timings: dict[str, int] | None = None,
) -> dict[str, list]:
    """Candidates from the vertical's own sources, run concurrently: SearXNG
    categories, a site:-filtered web search, and extra providers. A source that
    fails or runs out of quota is skipped, never fatal."""
    sx = _searxng(engine)
    jobs: dict[str, object] = {}
    if sx is not None and v.categories:
        jobs[f"searxng:{v.categories}"] = sx.search_pages(
            query, v.categories, want, max_pages=pages, time_range=time_range, budget_s=budget_s
        )
    if v.name == "shopping" and sx is not None and site_target(query) is None:
        # "<item> price" finds store product pages (with the price in the
        # snippet) far more often than "buy <item>", which finds category pages.
        jobs["searxng:price"] = sx.search_category(
            price_query(query),
            "general",
            want,
            time_range=time_range,
            engine_timeout=engine.settings.shopping_price_query_timeout,
        )
    if v.sites and site_target(query) is None:
        chosen = pick_sites(v, raw_query or query)  # before "instagram" etc. were stripped
        jobs["searxng:sites"] = _sites_search(
            engine,
            sx,
            query,
            chosen,
            want,
            time_range,
            pages=pages,
            budget_s=budget_s,
            url_ok=v.url_ok,
        )
    picked = pick_sites(v, raw_query or query) if v.sites else ()
    extra_providers = [*v.providers, *(n for site, n in v.site_providers if site in picked)]
    for name in extra_providers:
        p = engine.vertical_providers.get(name)
        if p is None or await try_provider(engine.quota, p) is not None:
            continue
        jobs[p.name] = p.search(raw_query or query, want, time_range=time_range)
    if not jobs:
        return {}
    names = list(jobs)

    async def timed(name: str, job):
        t = time.perf_counter()
        try:
            return await asyncio.wait_for(job, budget_s)
        finally:
            if timings is not None:
                timings[f"source:{name}"] = _ms(t)

    # Each source has its own deadline, so one slow source can't sink the rest.
    results = await asyncio.gather(*(timed(n, j) for n, j in jobs.items()), return_exceptions=True)
    out: dict[str, list] = {}
    for n, r in zip(names, results, strict=True):
        if isinstance(r, BaseException):
            log.info("vertical source %s failed: %r", n, r)
            if errors is not None:
                errors[n] = str(r) or type(r).__name__
            continue
        out[n] = r
    return out


async def _sites_search(
    engine: Engine,
    sx,
    query: str,
    sites: tuple[str, ...],
    want: int,
    time_range: str | None,
    *,
    pages: int,
    budget_s: float,
    url_ok=None,
) -> list:
    """A site:-filtered web search: SearXNG first, then (only if that fails or
    finds nothing) the first keyed web provider with quota left. Results not on
    the requested sites are dropped: some engines ignore site: filters."""
    ors = " OR ".join(f"site:{d}" for d in sites)
    q = f"{query} {ors}" if len(sites) == 1 else f"{query} ({ors})"
    found: list = []
    error: Exception | None = None

    def keep(url: str) -> bool:
        return any(on_site(url, s) for s in sites) and (url_ok is None or url_ok(url))

    if sx is not None:
        try:
            # Page until enough *usable* entries: site searches return many
            # pages that don't count (Instagram topic pages, LinkedIn posts).
            found = await sx.search_pages(
                q,
                "general",
                want,
                max_pages=max(pages, 2),
                time_range=time_range,
                budget_s=budget_s,
                keep=keep,
            )
        except Exception as e:
            error = e
    found = [c for c in found if any(on_site(c.url, s) for s in sites)]
    if not found:
        for p in engine.fallbacks:
            if p is sx or await try_provider(engine.quota, p) is not None:
                continue
            try:
                found = [
                    c
                    for c in await p.search(q, want, time_range=time_range)
                    if any(on_site(c.url, s) for s in sites)
                ]
            except Exception as e:
                error = error or e
                continue
            if found:
                break
    if not found and error is not None:
        raise error
    return found


# " | LinkedIn", " - YouTube", " • Instagram photos and videos": a site name on the title.
_TITLE_SUFFIX_RE = re.compile(
    r"\s+[|•·–—-]\s+(LinkedIn|YouTube|Instagram[^|]*|X|Twitter|GitHub|Crunchbase|Reddit|"
    r"Facebook|Vimeo|Threads|Bluesky|Lever|Greenhouse|Ashby|Indeed(\.com)?)\s*$",
    re.I,
)


def _strip_site_suffix(title: str) -> str:
    return _TITLE_SUFFIX_RE.sub("", title).strip() or title


def _fields_text(fields: dict | None) -> str:
    if not fields:
        return ""
    return " ".join(str(v) for v in fields.values() if isinstance(v, (str, int, float)))
