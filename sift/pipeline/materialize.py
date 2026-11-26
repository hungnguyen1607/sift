"""Turning candidate links into ranked passages.

The back half of a search: fetch pages (or load them from the local index and
page cache), chunk and embed them, and rank the passages against the query.
Every stage has a time budget and degrades (to snippets, to lexical-only
ranking) rather than failing when it runs out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import Executor
from typing import TYPE_CHECKING

from sift.pipeline.candidates import MergedCandidate, normalize_url
from sift.pipeline.chunk import chunk
from sift.pipeline.clean import MAX_TEXT_CHARS, clean_html, tidy_markdown
from sift.pipeline.embed import embedding_input
from sift.pipeline.fetch import FetchError
from sift.pipeline.fresh import apply_freshness
from sift.pipeline.models import Page, Passage
from sift.pipeline.pack import pack, truncate_tokens
from sift.pipeline.rank import Ranked, bm25_scores, fuse, select
from sift.pipeline.shared import _ms
from sift.providers.base import parse_ts

if TYPE_CHECKING:
    from sift.api.deps import Engine

log = logging.getLogger(__name__)

# A query embedding this far behind just means lexical-only ranking, no wait.
QUERY_EMBED_TIMEOUT = 1.0
# Passages embedded per live batch (bigger batches hurt tail latency).
_EMBED_BATCH = 4


# ----------------------------------------------------------------- ranking


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
    *,
    one_per_url: bool = False,
) -> tuple[list[Ranked], int | None]:
    t2 = time.perf_counter()
    ranked = await _rank_pool(engine, query, qvec, pool, depth)
    timings["embed"] = _ms(t2)
    ranked = apply_freshness(ranked, time_range, fresh_only)
    chosen = select(
        ranked, max_results, per_site=engine.settings.per_site_limit, one_per_url=one_per_url
    )
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


# ----------------------------------------------------- embeddings + the index


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
            if len(batch) == _EMBED_BATCH:
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


# ----------------------------------------------------------- fetch → pages


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
        cleaned = await _clean_html(engine.clean_executor, res.body, res.url)
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


async def _clean_html(executor: Executor | None, html: str, url: str) -> dict | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, clean_html, html, url)


# ---------------------------------------------------------- raw content + answer


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
