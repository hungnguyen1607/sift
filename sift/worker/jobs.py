from __future__ import annotations

import logging

import asyncpg

from sift.index.writer import unchanged, write_page
from sift.pipeline.chunk import chunk
from sift.pipeline.embed import Embedder, embedding_input
from sift.pipeline.models import Page
from sift.providers.base import parse_ts

log = logging.getLogger(__name__)
MAX_PASSAGES_PER_PAGE = 80


def page_payload(page: Page) -> dict:
    return {
        "url": page.url,
        "title": page.title,
        "text": page.text,
        "source": page.source,
        "published": page.published.isoformat() if page.published else None,
        "updated": page.updated.isoformat() if page.updated else None,
    }


async def index_page(conn: asyncpg.Connection, embedder: Embedder, payload: dict) -> str:
    page = Page(
        url=payload["url"],
        title=payload.get("title") or payload["url"],
        text=payload["text"],
        source=payload.get("source", "live"),
        published=parse_ts(payload.get("published")),
        updated=parse_ts(payload.get("updated")),
    )
    if await unchanged(conn, page):
        return "unchanged"
    spans = chunk(page.text)[:MAX_PASSAGES_PER_PAGE]
    if not spans:
        return "empty"
    vectors = await embedder.embed_passages(
        [embedding_input(page.title, s.heading, page.text[s.start : s.end]) for s in spans]
    )
    await write_page(conn, page, spans, vectors)
    return f"indexed {len(spans)} passages"


HANDLERS = {"index_page": index_page}
