"""One search, end to end:

candidates → fetch/clean (or load from the index) → chunk → rank → select,
then hand the newly fetched pages to the background index writer.
Every stage has a time budget; a stage that runs out degrades (to snippets,
to lexical-only ranking) instead of failing the request.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sift.pipeline.candidates import (
    RRF_K,
    CandidateResult,
    MergedCandidate,
    domain_matches,
    gather_candidates,
    normalize_url,
)
from sift.pipeline.chunk import chunk
from sift.pipeline.clean import MAX_TEXT_CHARS, clean_html, tidy_markdown
from sift.pipeline.embed import embedding_input
from sift.pipeline.fetch import FetchError
from sift.pipeline.fresh import apply_freshness
from sift.pipeline.keywords import has_recency_words
from sift.pipeline.models import Page, Passage
from sift.pipeline.pack import pack, truncate_tokens
from sift.pipeline.plan import plan_queries
from sift.pipeline.rank import Ranked, bm25_scores, fuse, select
from sift.pipeline.structured import job_fields, product_fields
from sift.pipeline.verticals import (
    VERTICALS,
    Vertical,
    coverage,
    detect_vertical,
    drop_far_places,
    key_terms,
    named_sites,
    on_site,
    pick_sites,
    price_cap,
    product_url,
    requested_count,
    search_query,
    site_target,
)
from sift.providers.base import parse_ts

if TYPE_CHECKING:
    from sift.api.deps import Engine

log = logging.getLogger(__name__)
QUERY_EMBED_TIMEOUT = 1.0


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


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


@dataclass(slots=True)
class _Sub:
    query: str
    qvec: list[float] | None
    local: list[Passage]
    cres: CandidateResult


async def _gather_sub(
    engine: Engine,
    query: str,
    *,
    allow_fallback: bool,
    depth: str,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    time_range: str | None,
    vertical: Vertical | None = None,
) -> _Sub:
    cfg = engine.settings
    # Exclusive only when its engines exist; without SearXNG, fall back to the
    # normal sources rather than return nothing.
    exclusive = vertical is not None and vertical.exclusive and _searxng(engine) is not None
    qvec = await _embed_query(engine, query)
    local = [] if exclusive else await _local_search(engine, query, qvec)
    extra = None
    errors: dict[str, str] = {}
    if vertical is not None and (vertical.categories or vertical.providers or vertical.sites):
        extra = _vertical_candidates(
            engine,
            search_query(vertical, query),
            vertical,
            cfg.candidates_per_provider,
            time_range,
            pages=1,
            # Exclusive verticals depend on these sources; for the others they
            # are optional extras and mustn't hold the whole answer up.
            budget_s=cfg.vertical_budget if vertical.exclusive else cfg.vertical_extra_budget,
            errors=errors,
            raw_query=query,
        )
    cres = await gather_candidates(
        query,
        [] if exclusive else engine.providers,
        engine.quota,
        per_provider=cfg.candidates_per_provider,
        budget_s=cfg.provider_timeout,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        fallbacks=(engine.fallbacks if allow_fallback and not exclusive else []),
        depth=depth,
        time_range=time_range,
        search_web=cfg.search_web,
        extra=extra,
        extra_budget_s=(cfg.vertical_budget if exclusive else cfg.vertical_extra_budget) + 0.5,
    )
    cres.providers_skipped.update(errors)
    if exclusive:
        # Off-topic entries from the vertical's engines (Wikinews lists its
        # latest stories whatever the query) would crowd out real matches.
        terms = key_terms(vertical, query)
        cres.candidates = [
            m
            for m in cres.candidates
            if coverage(vertical, terms, f"{m.candidate.title} {m.candidate.snippet}") >= 0.5
        ]
    return _Sub(query, qvec, local, cres)


async def run_search(
    engine: Engine,
    query: str,
    *,
    max_results: int = 5,
    max_tokens: int | None = None,
    depth: str = "basic",
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    include_raw_content: bool = False,
    fresh_only: bool = False,
    time_range: str | None = None,
    include_answer: bool = False,
    vertical: str = "auto",
) -> SearchOutcome:
    name = detect_vertical(query) if vertical in ("auto", None) else vertical
    v = VERTICALS.get(name, VERTICALS["web"])
    if v.shape == "list":
        outcome = await _run_vertical(
            engine,
            query,
            v,
            max_results=requested_count(query) or max_results,
            max_tokens=max_tokens,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            fresh_only=fresh_only,
            time_range=time_range,
        )
        outcome.vertical = v.name
        return outcome
    max_results = min(max_results, engine.settings.answer_max_results)
    if v.name == "news" and time_range is None and has_recency_words(query):
        time_range = "month"  # "latest X news": last year's story isn't the latest
    sub_queries = await plan_queries(engine, query) if v.name == "web" else [query]
    multi = len(sub_queries) > 1
    if multi:
        outcome = await _run_multi(
            engine,
            sub_queries,
            max_results=max_results,
            max_tokens=max_tokens,
            depth=depth,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            include_raw_content=include_raw_content,
            fresh_only=fresh_only,
            time_range=time_range,
        )
        outcome.sub_queries = sub_queries
    else:
        outcome = await _run_single(
            engine,
            query,
            max_results=max_results,
            max_tokens=max_tokens,
            depth=depth,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            include_raw_content=include_raw_content,
            fresh_only=fresh_only,
            time_range=time_range,
            vertical=None if v.name == "web" else v,
        )
    outcome.vertical = v.name
    if include_answer:
        outcome.answer = await _answer(engine, query, outcome.results)
    return outcome


async def _run_single(
    engine: Engine,
    query: str,
    *,
    max_results: int,
    max_tokens: int | None,
    depth: str,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    include_raw_content: bool,
    fresh_only: bool,
    time_range: str | None,
    vertical: Vertical | None = None,
) -> SearchOutcome:
    cfg = engine.settings
    timings: dict[str, int] = {}
    t0 = time.perf_counter()

    sub = await _gather_sub(
        engine,
        query,
        allow_fallback=True,
        depth=depth,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        time_range=time_range,
        vertical=vertical,
    )
    cres, qvec = sub.cres, sub.qvec
    timings["candidates"] = _ms(t0)

    def domain_ok(url: str) -> bool:
        if include_domains and not domain_matches(url, include_domains):
            return False
        return not (exclude_domains and domain_matches(url, exclude_domains))

    local_hits = [p for p in sub.local if domain_ok(p.page.url)]

    n_fetch = cfg.fetch_pages_advanced if depth == "advanced" else cfg.fetch_pages_basic
    top = cres.candidates[: n_fetch * 2]
    known = await _known_pages(engine, [m.candidate.url for m in top])
    pool: list[Passage] = list(local_hits)
    to_get: list[MergedCandidate] = []
    for m in top:
        passages = known.get(normalize_url(m.candidate.url))
        if passages:
            passages[0].page.sources = ["local", *m.sources]
            pool.extend(passages)
        elif len(to_get) < n_fetch:
            to_get.append(m)

    t1 = time.perf_counter()
    fetch_budget = cfg.fetch_budget_basic if depth == "basic" else cfg.fetch_budget
    pages = await _materialize(engine, to_get, fetch_budget)
    if vertical is not None and vertical.parse is not None:
        by_url = {normalize_url(m.candidate.url): m.candidate for m in to_get}
        for page in pages:
            c = by_url.get(normalize_url(page.url))
            if c is not None:
                page.fields = vertical.parse(c) or None
    timings["fetch"] = _ms(t1)
    for name, ms in cres.timings_ms.items():
        log.info("provider %s: %s ms", name, ms)

    pool.extend(_passages(pages))
    chosen, tokens_used = await _rank(
        engine, query, qvec, pool, max_results, max_tokens, depth, fresh_only, time_range, timings
    )

    engine.schedule_index([p for p in pages if p.origin == "live"])
    engine.schedule_touch([r.passage.page.url for r in chosen])

    texts = await _raw_content(engine, chosen, include_raw_content)
    used = list(cres.providers_used)
    if any(p.page.origin == "index" for p in pool):
        used.insert(0, "local")
    return SearchOutcome(
        results=chosen,
        providers_used=used,
        providers_skipped=cres.providers_skipped,
        cache_hit=bool(chosen) and not to_get,
        timings_ms=timings,
        page_texts=texts,
        tokens_used=tokens_used,
    )


async def _run_multi(
    engine: Engine,
    sub_queries: list[str],
    *,
    max_results: int,
    max_tokens: int | None,
    depth: str,
    include_domains: list[str] | None,
    exclude_domains: list[str] | None,
    include_raw_content: bool,
    fresh_only: bool,
    time_range: str | None,
) -> SearchOutcome:
    cfg = engine.settings
    timings: dict[str, int] = {}
    t0 = time.perf_counter()

    subs = await asyncio.gather(
        *(
            _gather_sub(
                engine,
                q,
                allow_fallback=(i == 0),
                depth=depth,
                include_domains=include_domains,
                exclude_domains=exclude_domains,
                time_range=time_range,
            )
            for i, q in enumerate(sub_queries)
        )
    )
    timings["candidates"] = _ms(t0)

    def domain_ok(url: str) -> bool:
        if include_domains and not domain_matches(url, include_domains):
            return False
        return not (exclude_domains and domain_matches(url, exclude_domains))

    for s in subs:
        s.local = [p for p in s.local if domain_ok(p.page.url)]

    n_fetch = cfg.fetch_pages_advanced if depth == "advanced" else cfg.fetch_pages_basic

    # Shared fetch: round-robin over sub-queries so every side gets coverage,
    # deduplicated by URL, at most n_fetch pages in total.
    selected: list[tuple[int, MergedCandidate]] = []
    seen: set[str] = set()
    lists = [list(s.cres.candidates) for s in subs]
    while len(selected) < n_fetch * 2 and any(lists):
        progressed = False
        for si, lst in enumerate(lists):
            while lst:
                m = lst.pop(0)
                key = normalize_url(m.candidate.url)
                if key in seen:
                    continue
                seen.add(key)
                selected.append((si, m))
                progressed = True
                break
        if not progressed:
            break

    known = await _known_pages(engine, [m.candidate.url for _, m in selected])
    pools: list[list[Passage]] = [[*s.local] for s in subs]
    to_get: list[tuple[int, MergedCandidate]] = []
    for si, m in selected:
        passages = known.get(normalize_url(m.candidate.url))
        if passages:
            passages[0].page.sources = ["local", *m.sources]
            pools[si].extend(passages)
        elif len(to_get) < n_fetch:
            to_get.append((si, m))

    t1 = time.perf_counter()
    fetch_budget = cfg.fetch_budget_basic if depth == "basic" else cfg.fetch_budget
    pages = await _materialize(engine, [m for _, m in to_get], fetch_budget)
    timings["fetch"] = _ms(t1)

    for i, page in enumerate(pages):
        pools[to_get[i][0]].extend(_passages([page]))

    t2 = time.perf_counter()
    ranked_per_sub: list[list[Ranked]] = []
    embed_ms = 0
    for s, pool in zip(subs, pools, strict=True):
        te = time.perf_counter()
        ranked = await _rank_pool(engine, s.query, s.qvec, pool, depth)
        embed_ms += _ms(te)
        ranked_per_sub.append(ranked)
    merged = _round_robin(ranked_per_sub)
    merged = apply_freshness(merged, time_range, fresh_only)
    chosen = select(merged, max_results, per_site=cfg.per_site_limit)
    chosen, tokens_used = pack(chosen, max_tokens)
    timings["embed"] = embed_ms
    timings["rank"] = _ms(t2) - embed_ms

    engine.schedule_index([p for p in pages if p.origin == "live"])
    engine.schedule_touch([r.passage.page.url for r in chosen])

    texts = await _raw_content(engine, chosen, include_raw_content)
    used: list[str] = []
    for s in subs:
        for name in s.cres.providers_used:
            if name not in used:
                used.append(name)
    if any(p.page.origin == "index" for pool in pools for p in pool):
        used.insert(0, "local")
    skipped: dict[str, str] = {}
    for s in subs:
        for k, v in s.cres.providers_skipped.items():
            skipped.setdefault(k, v)
    return SearchOutcome(
        results=chosen,
        providers_used=used,
        providers_skipped=skipped,
        cache_hit=bool(chosen) and not to_get,
        timings_ms=timings,
        page_texts=texts,
        tokens_used=tokens_used,
    )


def _searxng(engine: Engine):
    from sift.providers.searxng import SearXNGProvider

    return next((p for p in engine.fallbacks if isinstance(p, SearXNGProvider)), None)


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
        if p is None:
            continue
        if p.rate_limit is not None and not await engine.quota.try_rate(p.name, *p.rate_limit):
            continue
        if not await engine.quota.try_acquire(p.name):
            continue
        jobs[p.name] = p.search(raw_query or query, want, time_range=time_range)
    if not jobs:
        return {}
    names = list(jobs)
    # Each source has its own deadline, so one slow source can't sink the rest.
    results = await asyncio.gather(
        *(asyncio.wait_for(j, budget_s) for j in jobs.values()), return_exceptions=True
    )
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
            if p is sx:
                continue
            if p.rate_limit is not None and not await engine.quota.try_rate(p.name, *p.rate_limit):
                continue
            if not await engine.quota.try_acquire(p.name):
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


async def _run_vertical(
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
    timings["candidates"] = _ms(t0)

    # Entries that aren't the kind asked for (a LinkedIn post in a people search),
    # then one entry per canonical URL.
    merged: list[MergedCandidate] = []
    seen: set[str] = set()
    for m in cres.candidates:
        if v.name == "social" and m.candidate.extra.get("discussion_url"):
            m.candidate.url = m.candidate.extra["discussion_url"]  # the thread, not the article
        url = m.candidate.url
        if v.url_ok is not None and not v.url_ok(url):
            continue
        if v.canonical is not None:
            m.candidate.url = url = v.canonical(url)
        key = normalize_url(url)
        if key in seen:
            continue
        seen.add(key)
        merged.append(m)

    t1 = time.perf_counter()
    structured: dict[str, dict] = {}
    if v.fetch:
        todo = [m for m in merged if "job" not in m.candidate.extra]
        if v.name == "shopping":  # product pages carry the price; listings don't
            todo.sort(key=lambda m: not product_url(m.candidate.url))
        todo = todo[: v.fetch]
        structured = await _fetch_structured(engine, v, todo, cfg.fetch_budget_basic)
        timings["fetch"] = _ms(t1)

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
        fields.update(structured.get(normalize_url(c.url), {}))
        page.fields = {k: val for k, val in fields.items() if val not in (None, "", [])} or None
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

    t2 = time.perf_counter()
    pool = _passages(pool_pages)
    hays = [f"{p.page.title} {p.text} {_fields_text(p.page.fields)}" for p in pool]
    lexical = bm25_scores(" ".join(terms) or q, hays)
    lex_rank = {
        i: r
        for r, i in enumerate(
            sorted((i for i, s in enumerate(lexical) if s > 0), key=lambda i: -lexical[i]), 1
        )
    }
    # Entries with every key term first; within a tier, the sources' own order
    # (SearXNG's merged engine ranking) and keyword relevance, fused as RRF.
    # A platform the query names ("reddit posts about …") comes before others.
    named = named_sites(v, query)
    ranked = []
    for i, p in enumerate(pool):
        rrf = 1 / (RRF_K + i + 1) + (1 / (RRF_K + lex_rank[i]) if i in lex_rank else 0.0)
        cov = 1.0 if v.rank_by_source else coverage(v, terms, hays[i])
        if named and any(on_site(p.page.url, s) for s in named):
            cov += 1.0
        if v.name == "shopping":  # a priced product beats a category page
            cov += 0.5 * ("price" in (p.page.fields or {})) + 0.25 * product_url(p.page.url)
        if v.rank_by_source:
            rrf = 1 / (RRF_K + i + 1)
        r = Ranked(p, cov + rrf, lex_rank.get(i))
        ranked.append(r)
    ranked.sort(key=lambda r: -r.score)
    ranked = apply_freshness(ranked, time_range, fresh_only)
    # Entries share boilerplate ("Experience: X · Education: Y"), so dedupe by
    # URL only (a threshold above 1 turns the text check off).
    chosen = select(ranked, want, per_site=v.per_site or want, dup_threshold=1.01)
    chosen, tokens_used = pack(chosen, max_tokens)
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


async def _fetch_structured(
    engine: Engine, v: Vertical, todo: list[MergedCandidate], budget_s: float
) -> dict[str, dict]:
    """Fetch pages (robots.txt checked by the fetcher) and read their JSON-LD.
    Whatever doesn't finish within the budget keeps its snippet only."""
    if engine.fetcher is None or not todo:
        return {}
    reader = job_fields if v.name == "jobs" else product_fields

    async def one(url: str) -> tuple[str, dict]:
        res = await engine.fetcher.fetch(url)
        return normalize_url(url), reader(res.body) if res.is_html else {}

    tasks = [asyncio.create_task(one(m.candidate.url)) for m in todo]
    done, pending = await asyncio.wait(tasks, timeout=budget_s)
    for t in pending:
        t.cancel()
    out: dict[str, dict] = {}
    for t in done:
        if t.exception() is None:
            key, fields = t.result()
            if fields:
                if v.name == "shopping":
                    fields["price_checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                out[key] = fields
    return out


def _fields_text(fields: dict | None) -> str:
    if not fields:
        return ""
    return " ".join(str(v) for v in fields.values() if isinstance(v, (str, int, float)))


# " | LinkedIn", " - YouTube", " • Instagram photos and videos": a site name on the title.
_TITLE_SUFFIX_RE = re.compile(
    r"\s+[|•·–—-]\s+(LinkedIn|YouTube|Instagram[^|]*|X|Twitter|GitHub|Crunchbase|Reddit|"
    r"Facebook|Vimeo|Threads|Bluesky|Lever|Greenhouse|Ashby|Indeed(\.com)?)\s*$",
    re.I,
)


def _strip_site_suffix(title: str) -> str:
    return _TITLE_SUFFIX_RE.sub("", title).strip() or title


def _round_robin(ranked_lists: list[list[Ranked]]) -> list[Ranked]:
    """Interleave sub-query rankings so every side contributes before any side
    gets a second result."""
    out: list[Ranked] = []
    lists = [list(r) for r in ranked_lists]
    while any(lists):
        for lst in lists:
            if lst:
                out.append(lst.pop(0))
    return out


async def _rank_pool(
    engine: Engine, query: str, qvec, pool: list[Passage], depth: str
) -> list[Ranked]:
    lexical = bm25_scores(query, [f"{p.page.title} {p.heading or ''} {p.text}" for p in pool])
    if qvec is not None:
        await _embed_live(engine, pool, lexical)
    ranked = fuse(pool, lexical, qvec)
    if depth == "advanced" and engine.reranker is not None:
        await _rerank_top(engine, query, ranked)
    return ranked


async def _rank(
    engine: Engine,
    query: str,
    qvec,
    pool: list[Passage],
    max_results: int,
    max_tokens: int | None,
    depth: str,
    fresh_only: bool,
    time_range: str | None,
    timings: dict[str, int],
) -> tuple[list[Ranked], int | None]:
    t2 = time.perf_counter()
    ranked = await _rank_pool(engine, query, qvec, pool, depth)
    timings["embed"] = _ms(t2)
    ranked = apply_freshness(ranked, time_range, fresh_only)
    chosen = select(ranked, max_results, per_site=engine.settings.per_site_limit)
    chosen, tokens_used = pack(chosen, max_tokens)
    timings["rank"] = _ms(t2) - timings["embed"]
    return chosen, tokens_used


def _passages(pages: list[Page]) -> list[Passage]:
    out: list[Passage] = []
    for page in pages:
        if page.origin == "snippet":
            out.append(Passage(page, page.text, 0, len(page.text)))
            continue
        for s in chunk(page.text):
            out.append(Passage(page, page.text[s.start : s.end], s.start, s.end, s.heading))
    return out


async def _raw_content(
    engine: Engine, chosen: list[Ranked], include_raw_content: bool
) -> dict[str, str]:
    if not include_raw_content:
        return {}
    texts: dict[str, str] = {}
    for r in chosen:
        if r.passage.page.text:
            texts[normalize_url(r.passage.page.url)] = r.passage.page.text
    missing = [r.passage.page.url for r in chosen if not r.passage.page.text]
    if missing and engine.index is not None:
        texts.update(await engine.index.page_texts(missing))
    return texts


async def _answer(engine: Engine, query: str, results: list[Ranked]) -> str | None:
    from sift.pipeline.answer import generate_answer

    try:
        return await generate_answer(engine, query, results)
    except Exception as e:
        log.warning("answer generation failed: %r", e)
        return None


async def _rerank_top(engine: Engine, query: str, ranked: list[Ranked]) -> None:
    """Rescore the top passages with a cross-encoder (advanced only), keeping
    whatever finished within the budget; the rest keep their RRF order."""
    cfg = engine.settings
    n = min(cfg.rerank_passages, len(ranked))
    if n == 0:
        return
    top = ranked[:n]
    docs = [
        truncate_tokens(embedding_input(p.page.title, p.heading, p.text), cfg.rerank_tokens)
        for p in (r.passage for r in top)
    ]
    scores = await engine.reranker.rerank(query, docs, cfg.rerank_budget)
    completed = sorted(
        ((r, s) for r, s in zip(top, scores, strict=False) if s is not None),
        key=lambda x: -x[1],
    )
    uncompleted = [r for r, s in zip(top, scores, strict=False) if s is None]
    for r, s in completed:
        r.score = s
    ranked[:n] = [r for r, _ in completed] + uncompleted


async def _embed_query(engine: Engine, query: str) -> list[float] | None:
    if engine.embedder is None:
        return None
    try:
        return await asyncio.wait_for(engine.embedder.embed_query(query), QUERY_EMBED_TIMEOUT)
    except Exception as e:  # a missing vector just means lexical-only ranking
        log.warning("query embedding failed: %r", e)
        return None


async def _local_search(engine: Engine, query: str, qvec) -> list[Passage]:
    if engine.index is None:
        return []
    try:
        return await engine.index.search(query, qvec, engine.settings.local_candidates)
    except Exception as e:
        log.warning("local index search failed: %r", e)
        return []


async def _known_pages(engine: Engine, urls: list[str]) -> dict[str, list[Passage]]:
    if engine.index is None:
        return {}
    try:
        return await engine.index.known_pages(urls)
    except Exception as e:
        log.warning("local index lookup failed: %r", e)
        return {}


async def _embed_live(engine: Engine, pool: list[Passage], lexical: list[float]) -> None:
    """Embed the opening words of the best lexical live passages, best first,
    in small batches, starting only batches that should finish within budget.
    Whatever is embedded when the budget runs out gets used; the rest rank
    lexically."""
    cfg = engine.settings
    emb = engine.embedder
    todo = sorted(
        (i for i, p in enumerate(pool) if p.embedding is None and p.page.origin != "index"),
        key=lambda i: -lexical[i],
    )[: cfg.embed_live_passages]
    deadline = time.perf_counter() + cfg.embed_budget
    queue = list(todo)
    done = 0
    while queue:
        remaining_ms = (deadline - time.perf_counter()) * 1000
        batch: list[int] = []
        texts: list[str] = []
        for i in list(queue):
            p = pool[i]
            text = embedding_input(p.page.title, p.heading, p.text, cfg.embed_live_words)
            if emb.estimate_ms([*texts, text]) <= remaining_ms:
                batch.append(i)
                texts.append(text)
                queue.remove(i)
            elif not batch:
                queue.remove(i)  # can never fit now; try a shorter one
            if len(batch) == 4:
                break
        if not batch:
            break
        try:
            vecs = await asyncio.wait_for(
                emb.embed_passages(texts), max(remaining_ms / 1000, 0.05) + 0.2
            )
        except TimeoutError:
            break
        for i, v in zip(batch, vecs, strict=True):
            pool[i].embedding = v
        done += len(batch)
    if done < len(todo):
        log.info("live embedding: %s/%s passages within budget", done, len(todo))


async def _materialize(engine: Engine, cands: list[MergedCandidate], budget_s: float) -> list[Page]:
    """Turn candidate links into pages within the fetch budget. A link that
    can't be fetched in time falls back to its provider snippet."""
    if not cands:
        return []
    tasks = {asyncio.create_task(_get_page(engine, m)): m for m in cands}
    done, pending = await asyncio.wait(tasks, timeout=budget_s)
    for t in pending:
        t.cancel()
    pages = []
    for t, m in tasks.items():
        page = None
        if t in done and t.exception() is None:
            page = t.result()
        elif t in done:
            log.info("fetch %s failed: %s", m.candidate.url, t.exception())
        if page is None:
            page = _snippet_page(m)
        if page is not None:
            page.sources = list(m.sources)
            pages.append(page)
    return pages


def _snippet_page(m: MergedCandidate) -> Page | None:
    c = m.candidate
    if not c.snippet:
        return None
    return Page(c.url, c.title, c.snippet, c.source, c.published, c.updated, origin="snippet")


async def _get_page(engine: Engine, m: MergedCandidate) -> Page | None:
    c = m.candidate
    if c.content:
        return Page(c.url, c.title, tidy_markdown(c.content), c.source, c.published, c.updated)

    cache = engine.page_cache
    norm = normalize_url(c.url)
    if cache is not None:
        cached = await cache.get(norm)
        if cached is not None:
            return Page(
                cached.get("url", c.url),
                cached.get("title") or c.title or c.url,
                cached.get("raw_content", ""),
                cached.get("source", c.source),
                parse_ts(cached.get("published")),
                parse_ts(cached.get("updated")),
            )

    owners = [*engine.providers, *engine.vertical_providers.values()]
    owner = next((p for p in owners if p.owns(c.url)), None)
    if owner is not None:
        if not await engine.quota.try_acquire(owner.name):
            return None
        page = await owner.fetch_page(c.url)
        if page is not None:
            page.published = page.published or c.published
            page.updated = page.updated or c.updated
        return page

    try:
        res = await engine.fetcher.fetch(c.url)
    except FetchError as e:
        log.info("fetch %s: %s", c.url, e)
        return None
    if res.is_html:
        cleaned = await _clean(engine.clean_executor, res.body, res.url)
        if cleaned is None:
            return None
        text = cleaned["text"]
        title = c.title or cleaned["title"] or res.url
        published = parse_ts(cleaned["published"]) or c.published
        updated = parse_ts(cleaned["updated"]) or res.last_modified or c.updated
    else:
        text = tidy_markdown(res.body[:MAX_TEXT_CHARS])
        title, published, updated = c.title or res.url, c.published, res.last_modified or c.updated
    if not text.strip():
        return None
    # Keep the URL the provider gave: it's what the agent asked about, and
    # redirect targets often carry session junk.
    page = Page(c.url, title, text, c.source, published, updated)
    if cache is not None:
        await cache.set(
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
    return page


async def _clean(executor: Executor | None, html: str, url: str) -> dict | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, clean_html, html, url)
