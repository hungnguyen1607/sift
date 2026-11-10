"""Read side of the local index: hybrid passage search, and loading pages
that are already indexed so they don't need fetching again."""

from __future__ import annotations

import asyncpg

from sift.index.vectors import from_pg, to_pg
from sift.pipeline.candidates import normalize_url
from sift.pipeline.models import Page, Passage

# Full-text and HNSW vector search fused with RRF inside Postgres.
_HYBRID_SQL = """
WITH fts AS (
    SELECT id, row_number() OVER (ORDER BY rank DESC) AS r
    FROM (
        SELECT p.id, ts_rank_cd(p.tsv, q) AS rank
        FROM passages p, websearch_to_tsquery('english', $1) AS q
        WHERE p.tsv @@ q
        ORDER BY rank DESC
        LIMIT $3
    ) t
), vec AS (
    -- LIMIT inside so the planner walks the HNSW index instead of scoring every row.
    SELECT id, row_number() OVER (ORDER BY dist) AS r
    FROM (
        SELECT id, embedding <=> $2::text::halfvec AS dist
        FROM passages
        ORDER BY embedding <=> $2::text::halfvec
        LIMIT $3
    ) t
), fused AS (
    SELECT id, COALESCE(1.0 / (60 + fts.r), 0) + COALESCE(1.0 / (60 + vec.r), 0) AS rrf
    FROM fts FULL OUTER JOIN vec USING (id)
)
SELECT ps.content, ps.char_start, ps.char_end, ps.heading, ps.embedding::text AS emb,
       pg.url, pg.title, pg.source, pg.published, pg.updated
FROM fused f
JOIN passages ps ON ps.id = f.id
JOIN pages pg ON pg.id = ps.page_id
ORDER BY f.rrf DESC
LIMIT $3
"""
_FTS_ONLY_SQL = """
SELECT ps.content, ps.char_start, ps.char_end, ps.heading, ps.embedding::text AS emb,
       pg.url, pg.title, pg.source, pg.published, pg.updated
FROM passages ps JOIN pages pg ON pg.id = ps.page_id, websearch_to_tsquery('english', $1) q
WHERE ps.tsv @@ q
ORDER BY ts_rank_cd(ps.tsv, q) DESC
LIMIT $2
"""
_KNOWN_SQL = """
SELECT pg.url_norm, pg.url, pg.title, pg.source, pg.published, pg.updated,
       ps.content, ps.char_start, ps.char_end, ps.heading, ps.embedding::text AS emb
FROM pages pg JOIN passages ps ON ps.page_id = pg.id
WHERE pg.url_norm = ANY($1::text[])
ORDER BY pg.id, ps.ord
"""


class LocalIndex:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def search(
        self, query: str, query_vec: list[float] | None, limit: int = 30
    ) -> list[Passage]:
        async with self.pool.acquire() as conn:
            if query_vec is None:
                rows = await conn.fetch(_FTS_ONLY_SQL, query, limit)
            else:
                rows = await conn.fetch(_HYBRID_SQL, query, to_pg(query_vec), limit)
        pages: dict[str, Page] = {}
        out = []
        for r in rows:
            page = pages.get(r["url"])
            if page is None:
                page = pages[r["url"]] = _page(r)
            out.append(_passage(page, r))
        return out

    async def page_texts(self, urls: list[str]) -> dict[str, str]:
        """url_norm → full clean text (the API's raw_content)."""
        if not urls:
            return {}
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT url_norm, content FROM pages WHERE url_norm = ANY($1::text[])",
                list({normalize_url(u) for u in urls}),
            )
        return {r["url_norm"]: r["content"] for r in rows}

    async def extractable(self, urls: list[str], max_age_days: int = 7) -> dict[str, dict]:
        """url_norm → {url, title, raw_content, published, updated} for pages
        indexed recently enough that the stored copy is still fresh."""
        if not urls:
            return {}
        norms = list({normalize_url(u) for u in urls})
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT url_norm, url, title, content, published, updated
                FROM pages
                WHERE url_norm = ANY($1::text[]) AND fetched_at > now() - make_interval(days => $2)
                """,
                norms,
                max_age_days,
            )
        return {
            r["url_norm"]: {
                "url": r["url"],
                "title": r["title"] or r["url"],
                "raw_content": r["content"],
                "published": r["published"],
                "updated": r["updated"],
            }
            for r in rows
        }

    async def known_pages(self, urls: list[str]) -> dict[str, list[Passage]]:
        """url_norm → that page's indexed passages, for the given URLs."""
        if not urls:
            return {}
        norms = list({normalize_url(u) for u in urls})
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(_KNOWN_SQL, norms)
        out: dict[str, list[Passage]] = {}
        pages: dict[str, Page] = {}
        for r in rows:
            key = r["url_norm"]
            if key not in pages:
                pages[key] = _page(r)
            out.setdefault(key, []).append(_passage(pages[key], r))
        return out


def _page(r) -> Page:
    return Page(
        url=r["url"],
        title=r["title"] or r["url"],
        text="",  # loaded on demand by page_texts(); pages can be 200 KB
        source=r["source"],
        published=r["published"],
        updated=r["updated"],
        origin="index",
        sources=["local"],
    )


def _passage(page: Page, r) -> Passage:
    return Passage(
        page=page,
        text=r["content"],
        start=r["char_start"],
        end=r["char_end"],
        heading=r["heading"],
        embedding=from_pg(r["emb"]),
    )
