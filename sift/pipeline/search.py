"""One search, end to end:

candidates → fetch/clean (or load from the index) → chunk → rank → select,
then hand the newly fetched pages to the background index writer.
Every stage has a time budget; a stage that runs out degrades (to snippets,
to lexical-only ranking) instead of failing the request.

This module is the orchestrator. The stages live alongside it:
:mod:`.listing` (list verticals), :mod:`.materialize` (fetch → ranked
passages), :mod:`.shopping` (prices), with shared types in :mod:`.shared`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sift.pipeline.candidates import (
    CandidateResult,
    MergedCandidate,
    domain_matches,
    gather_candidates,
    normalize_url,
)
from sift.pipeline.fresh import apply_freshness
from sift.pipeline.keywords import has_recency_words
from sift.pipeline.listing import _vertical_candidates, run_vertical
from sift.pipeline.materialize import (
    _answer,
    _embed_query,
    _known_pages,
    _local_search,
    _materialize,
    _passages,
    _rank,
    _rank_pool,
    _raw_content,
)
from sift.pipeline.models import Passage
from sift.pipeline.pack import pack
from sift.pipeline.plan import plan_queries
from sift.pipeline.rank import Ranked, select
from sift.pipeline.shared import SearchOutcome, _ms, _searxng
from sift.pipeline.verticals import (
    VERTICALS,
    Vertical,
    coverage,
    detect_vertical,
    key_terms,
    requested_count,
    search_query,
)
from sift.providers.base import Candidate

if TYPE_CHECKING:
    from sift.api.deps import Engine

log = logging.getLogger(__name__)

# Candidates fetched vs. wanted: fetch twice the final count so a few failed
# fetches or off-topic pages still leave enough to rank.
_OVERFETCH = 2
# An exclusive vertical's engines list off-topic entries (Wikinews dumps its
# latest stories whatever the query); keep only those covering half the terms.
_VERTICAL_COVERAGE_FLOOR = 0.5


@dataclass(slots=True)
class _Sub:
    query: str
    qvec: list[float] | None
    local: list[Passage]
    cres: CandidateResult


def _domain_ok(url: str, include: list[str] | None, exclude: list[str] | None) -> bool:
    if include and not domain_matches(url, include):
        return False
    return not (exclude and domain_matches(url, exclude))


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
    src_timings: dict[str, int] = {}
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
            timings=src_timings,
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
        hedge_s=cfg.fallback_hedge,
    )
    cres.providers_skipped.update(errors)
    cres.timings_ms.update(src_timings)
    if exclusive:
        terms = key_terms(vertical, query)
        cres.candidates = [
            m
            for m in cres.candidates
            if coverage(vertical, terms, f"{m.candidate.title} {m.candidate.snippet}")
            >= _VERTICAL_COVERAGE_FLOOR
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
    if vertical in ("auto", None):
        name = detect_vertical(query)
        if engine.vertical_knn is not None:
            name = await engine.vertical_knn.route(query, name)
        auto = True
    else:
        name = vertical
        auto = False
    v = VERTICALS.get(name, VERTICALS["web"])
    if v.shape == "list":
        outcome = await run_vertical(
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
        # Web floor: a query the agent didn't pin to this vertical should never
        # come back empty just because the vertical's engines found nothing.
        # Retry as a plain web search so "search anything" always returns.
        if outcome.results or not auto:
            return outcome
        web = await run_search(
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
            include_answer=include_answer,
            vertical="web",
        )
        web.detected_vertical = v.name
        web.fell_back_to_web = True
        web.providers_skipped = {**outcome.providers_skipped, **web.providers_skipped}
        return web
    max_results = min(max_results, engine.settings.answer_max_results)
    if v.name == "news" and time_range is None and has_recency_words(query):
        time_range = "month"  # "latest X news": last year's story isn't the latest
    sub_queries = await plan_queries(engine, query) if v.name == "web" else [query]
    if len(sub_queries) > 1:
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

    local_hits = [p for p in sub.local if _domain_ok(p.page.url, include_domains, exclude_domains)]

    n_fetch = cfg.fetch_pages_advanced if depth == "advanced" else cfg.fetch_pages_basic
    top = cres.candidates[: n_fetch * _OVERFETCH]
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
        # Per-source times, so a slow source is visible in the response.
        timings[name if name.startswith("source:") else f"source:{name}"] = ms

    pool.extend(_passages(pages))
    chosen, tokens_used = await _rank(
        engine,
        query,
        qvec,
        pool,
        max_results,
        max_tokens,
        depth,
        fresh_only,
        time_range,
        timings,
        one_per_url=vertical is not None and vertical.name == "news",
    )
    if vertical is not None and vertical.parse is not None:
        # Pages from the local index (or the page cache) skipped the parse
        # above; give them the same fields (a news article's publisher/date).
        for r in chosen:
            page = r.passage.page
            if page.fields is None:
                c = Candidate(
                    url=page.url,
                    title=page.title,
                    snippet="",
                    source=page.source,
                    published=page.published,
                )
                page.fields = vertical.parse(c) or None

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

    for s in subs:
        s.local = [p for p in s.local if _domain_ok(p.page.url, include_domains, exclude_domains)]

    n_fetch = cfg.fetch_pages_advanced if depth == "advanced" else cfg.fetch_pages_basic

    # Shared fetch: round-robin over sub-queries so every side gets coverage,
    # deduplicated by URL, at most n_fetch pages in total.
    selected: list[tuple[int, MergedCandidate]] = []
    seen: set[str] = set()
    lists = [list(s.cres.candidates) for s in subs]
    while len(selected) < n_fetch * _OVERFETCH and any(lists):
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
        for k, reason in s.cres.providers_skipped.items():
            skipped.setdefault(k, reason)
    return SearchOutcome(
        results=chosen,
        providers_used=used,
        providers_skipped=skipped,
        cache_hit=bool(chosen) and not to_get,
        timings_ms=timings,
        page_texts=texts,
        tokens_used=tokens_used,
    )


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
