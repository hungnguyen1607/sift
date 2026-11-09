"""Write side of the local index. Only the background worker calls this."""

from __future__ import annotations

import hashlib

import asyncpg

from sift.index.vectors import to_pg
from sift.pipeline.candidates import normalize_url
from sift.pipeline.chunk import Span
from sift.pipeline.models import Page


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


async def unchanged(conn: asyncpg.Connection, page: Page) -> bool:
    """True if the page is indexed with the same text (just bump fetched_at)."""
    row = await conn.fetchrow(
        "UPDATE pages SET fetched_at = now() "
        "WHERE url_norm = $1 AND content_hash = $2 RETURNING id",
        normalize_url(page.url),
        content_hash(page.text),
    )
    return row is not None


async def write_page(
    conn: asyncpg.Connection, page: Page, spans: list[Span], embeddings: list[list[float]]
) -> int:
    """Upsert a page and replace its passages, all in one transaction."""
    assert len(spans) == len(embeddings)
    async with conn.transaction():
        page_id = await conn.fetchval(
            """
            INSERT INTO pages (url, url_norm, title, content, source, published, updated,
                               content_hash, fetched_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
            ON CONFLICT (url_norm) DO UPDATE SET
                url = EXCLUDED.url, title = EXCLUDED.title, content = EXCLUDED.content,
                source = EXCLUDED.source,
                published = COALESCE(EXCLUDED.published, pages.published),
                updated = COALESCE(EXCLUDED.updated, pages.updated),
                content_hash = EXCLUDED.content_hash, fetched_at = now()
            RETURNING id
            """,
            page.url,
            normalize_url(page.url),
            page.title,
            page.text,
            page.source,
            page.published,
            page.updated,
            content_hash(page.text),
        )
        await conn.execute("DELETE FROM passages WHERE page_id = $1", page_id)
        await conn.executemany(
            """
            INSERT INTO passages (page_id, ord, content, char_start, char_end, heading, embedding)
            VALUES ($1, $2, $3, $4, $5, $6, $7::text::halfvec)
            """,
            [
                (page_id, i, page.text[s.start : s.end], s.start, s.end, s.heading, to_pg(e))
                for i, (s, e) in enumerate(zip(spans, embeddings, strict=True))
            ],
        )
    return page_id
