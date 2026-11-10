"""Load a seed pack into the local index: verify, bulk ``COPY`` into staging
tables, upsert pages (live data wins when newer) and replace their passages,
then rebuild the HNSW index so a big load doesn't crawl under a live index.
"""

from __future__ import annotations

import logging
from pathlib import Path

import asyncpg
import pyarrow.parquet as pq

from sift.index.vectors import to_pg
from sift.pipeline.candidates import normalize_url
from sift.seeds.packs import PAGES_FILE, PASSAGES_FILE, verify_pack

log = logging.getLogger(__name__)

_COPY_BATCH = 10_000


async def _copy_pages(conn: asyncpg.Connection, path: Path) -> None:
    await conn.execute(
        "CREATE TEMP TABLE pages_staging (url text, url_norm text, title text, content text, "
        "source text, published timestamptz, updated timestamptz, content_hash text)"
    )
    pf = pq.ParquetFile(path)
    columns = [
        "url",
        "url_norm",
        "title",
        "content",
        "source",
        "published",
        "updated",
        "content_hash",
    ]
    for batch in pf.iter_batches(batch_size=_COPY_BATCH):
        rows = []
        for row in batch.to_pylist():
            norm = normalize_url(row["url"])
            rows.append(
                (
                    row["url"],
                    norm,
                    row["title"] or row["url"],
                    row["text"],
                    row["source"],
                    row["published"],
                    row["updated"],
                    _hash(row["text"]),
                )
            )
        await conn.copy_records_to_table("pages_staging", records=rows, columns=columns)
    await conn.execute(
        """
        INSERT INTO pages (url, url_norm, title, content, source, published, updated, content_hash)
        SELECT url, url_norm, title, content, source, published, updated, content_hash
        FROM pages_staging
        ON CONFLICT (url_norm) DO UPDATE SET
            url = EXCLUDED.url, title = EXCLUDED.title, content = EXCLUDED.content,
            source = EXCLUDED.source,
            published = COALESCE(EXCLUDED.published, pages.published),
            updated = COALESCE(EXCLUDED.updated, pages.updated),
            content_hash = EXCLUDED.content_hash, fetched_at = now()
        WHERE pages.updated IS NULL OR EXCLUDED.updated IS NULL OR EXCLUDED.updated >= pages.updated
        """
    )
    await conn.execute("DROP TABLE pages_staging")


async def _copy_passages(conn: asyncpg.Connection, path: Path) -> None:
    import numpy as np

    await conn.execute(
        "CREATE TEMP TABLE passages_staging (page_url text, ord int, content text, char_start int, "
        "char_end int, heading text, embedding text)"
    )
    pf = pq.ParquetFile(path)
    columns = ["page_url", "ord", "content", "char_start", "char_end", "heading", "embedding"]
    for batch in pf.iter_batches(batch_size=_COPY_BATCH):
        rows = []
        for row in batch.to_pylist():
            vec = np.frombuffer(row["embedding"], dtype=np.float16).astype(np.float32).tolist()
            rows.append(
                (
                    normalize_url(row["page_url"]),
                    row["ord"],
                    row["content"],
                    row["char_start"],
                    row["char_end"],
                    row["heading"],
                    to_pg(vec),
                )
            )
        await conn.copy_records_to_table("passages_staging", records=rows, columns=columns)

    await conn.execute(
        """
        DELETE FROM passages WHERE page_id IN (
            SELECT id FROM pages WHERE url_norm IN (SELECT DISTINCT page_url FROM passages_staging)
        )
        """
    )
    await conn.execute(
        """
        INSERT INTO passages (page_id, ord, content, char_start, char_end, heading, embedding)
        SELECT p.id, ps.ord, ps.content, ps.char_start, ps.char_end, ps.heading,
               ps.embedding::text::halfvec
        FROM passages_staging ps JOIN pages p ON p.url_norm = ps.page_url
        """
    )
    await conn.execute("DROP TABLE passages_staging")


async def rebuild_hnsw(pool: asyncpg.Pool) -> None:
    """Drop and rebuild the HNSW index with a bigger maintenance budget, so a
    large bulk load doesn't insert into a live index one row at a time."""
    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS passages_embedding_idx")
        await conn.execute("SET maintenance_work_mem = '1GB'")
        await conn.execute("SET max_parallel_maintenance_workers = 2")
        await conn.execute(
            "CREATE INDEX passages_embedding_idx ON passages USING hnsw "
            "(embedding halfvec_cosine_ops)"
        )


async def load_pack(pool: asyncpg.Pool, directory: Path, *, rebuild_index: bool = True) -> dict:
    directory = Path(directory)
    manifest = verify_pack(directory)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _copy_pages(conn, directory / PAGES_FILE)
            await _copy_passages(conn, directory / PASSAGES_FILE)
        if rebuild_index:
            await rebuild_hnsw(pool)

    log.info("loaded pack %s: %s passages", manifest.name, manifest.passage_count)
    return {"pack": manifest.name, "passages": manifest.passage_count}


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:32]
