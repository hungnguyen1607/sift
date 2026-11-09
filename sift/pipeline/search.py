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
from sift.pipeline.models import Page, Passage
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


def _ms(t0: float) -> int:
    return round((time.perf_counter() - t0) * 1000)


async def run_search(
    engine: Engine,
    query: str,
    *,
    max_results: int = 5,
    depth: str = "basic",
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    include_raw_content: bool = False,
) -> SearchOutcome:
    cfg = engine.settings
    timings: dict[str, int] = {}
    t0 = time.perf_counter()

    # 1. Candidates from every provider, the query embedding, then local passages.
    cand_task = asyncio.create_task(
        gather_candidates(
            query,
            engine.providers,
            engine.quota,
            per_provider=cfg.candidates_per_provider,
            budget_s=cfg.provider_timeout,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
        )
    )
    qvec = await _embed_query(engine, query)
    local_hits = await _local_search(engine, query, qvec)
    cres: CandidateResult = await cand_task
    timings["candidates"] = _ms(t0)

    def domain_ok(url: str) -> bool:
        if include_domains and not domain_matches(url, include_domains):
            return False
        return not (exclude_domains and domain_matches(url, exclude_domains))

    local_hits = [p for p in local_hits if domain_ok(p.page.url)]

    # 2. Pages: reuse indexed ones, fetch the rest.
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
    pages = await _materialize(engine, to_get)
    timings["fetch"] = _ms(t1)

    live: list[Passage] = []
    for page in pages:
        if page.origin == "snippet":
            live.append(Passage(page, page.text, 0, len(page.text)))
            continue
        for s in chunk(page.text):
            live.append(Passage(page, page.text[s.start : s.end], s.start, s.end, s.heading))
    pool.extend(live)

    # 3. Rank: BM25 over the whole pool, vectors where we have them.
    t2 = time.perf_counter()
    lexical = bm25_scores(query, [f"{p.page.title} {p.heading or ''} {p.text}" for p in pool])
    if qvec is not None:
        await _embed_live(engine, pool, lexical)
    timings["embed"] = _ms(t2)
    ranked = fuse(pool, lexical, qvec)
    chosen = select(ranked, max_results, per_site=cfg.per_site_limit)
    timings["rank"] = _ms(t2) - timings["embed"]

    # 4. Index what we fetched, in the background. Searches never wait on writes.
    engine.schedule_index([p for p in pages if p.origin == "live"])

    texts: dict[str, str] = {}
    if include_raw_content:
        for r in chosen:
            if r.passage.page.text:
                texts[normalize_url(r.passage.page.url)] = r.passage.page.text
        missing = [r.passage.page.url for r in chosen if not r.passage.page.text]
        if missing and engine.index is not None:
            texts.update(await engine.index.page_texts(missing))

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
    )


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


async def _materialize(engine: Engine, cands: list[MergedCandidate]) -> list[Page]:
    """Turn candidate links into pages within the fetch budget. A link that
    can't be fetched in time falls back to its provider snippet."""
    if not cands:
        return []
    tasks = {asyncio.create_task(_get_page(engine, m)): m for m in cands}
    done, pending = await asyncio.wait(tasks, timeout=engine.settings.fetch_budget)
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
    return Page(c.url, title, text, c.source, published, updated)


async def _clean(executor: Executor | None, html: str, url: str) -> dict | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, clean_html, html, url)
