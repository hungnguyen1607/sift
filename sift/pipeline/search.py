"""One search, end to end:

candidates → fetch/clean (or load from the index) → chunk → rank → select,
then hand the newly fetched pages to the background index writer.
Every stage has a time budget; a stage that runs out degrades (to snippets,
to lexical-only ranking) instead of failing the request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sift.pipeline.candidates import (
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
from sift.pipeline.models import Page, Passage
from sift.pipeline.pack import pack, truncate_tokens
from sift.pipeline.plan import plan_queries
from sift.pipeline.rank import Ranked, bm25_scores, fuse, select
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
) -> _Sub:
    cfg = engine.settings
    qvec = await _embed_query(engine, query)
    local = await _local_search(engine, query, qvec)
    cres = await gather_candidates(
        query,
        engine.providers,
        engine.quota,
        per_provider=cfg.candidates_per_provider,
        budget_s=cfg.provider_timeout,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        fallbacks=(engine.fallbacks if allow_fallback else []),
        depth=depth,
        time_range=time_range,
        search_web=cfg.search_web,
    )
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
) -> SearchOutcome:
    sub_queries = await plan_queries(engine, query)
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
        )
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

    owner = next((p for p in engine.providers if p.owns(c.url)), None)
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
