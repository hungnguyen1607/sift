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
    # Track which pages the pack actually wrote (inserted or newer-than-live), so
    # passages are only replaced for those — a newer live page keeps its data.
    await conn.execute("CREATE TEMP TABLE seed_written_pages (url_norm text PRIMARY KEY)")
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
    written = await conn.fetch(
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
        RETURNING url_norm
        """
    )
    if written:
        await conn.executemany(
            "INSERT INTO seed_written_pages (url_norm) VALUES ($1) ON CONFLICT DO NOTHING",
            [(r["url_norm"],) for r in written],
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
            SELECT p.id FROM pages p JOIN seed_written_pages w ON w.url_norm = p.url_norm
        )
        """
    )
    await conn.execute(
        """
        INSERT INTO passages (page_id, ord, content, char_start, char_end, heading, embedding)
        SELECT p.id, ps.ord, ps.content, ps.char_start, ps.char_end, ps.heading,
               ps.embedding::text::halfvec
        FROM passages_staging ps
        JOIN pages p ON p.url_norm = ps.page_url
        JOIN seed_written_pages w ON w.url_norm = ps.page_url
        """
    )
    await conn.execute("DROP TABLE passages_staging")


async def rebuild_hnsw(pool: asyncpg.Pool) -> None:
    """Drop and rebuild the HNSW index so a large bulk load doesn't insert into
    a live index row by row. Tries a parallel build first; if the container's
    shared memory is too small, falls back to a single-process build."""
    async with pool.acquire() as conn:
        await conn.execute("DROP INDEX IF EXISTS passages_embedding_idx")
        for workers, mem in ((2, "512MB"), (0, "256MB")):
            try:
                await conn.execute(f"SET maintenance_work_mem = '{mem}'")
                await conn.execute(f"SET max_parallel_maintenance_workers = {workers}")
                await conn.execute(
                    "CREATE INDEX passages_embedding_idx ON passages USING hnsw "
                    "(embedding halfvec_cosine_ops)"
                )
                return
            except asyncpg.PostgresError as e:
                log.warning("HNSW build failed (workers=%s, mem=%s): %s", workers, mem, e)
                await conn.execute("DROP INDEX IF EXISTS passages_embedding_idx")
        raise RuntimeError("could not build the HNSW index")


async def load_pack(pool: asyncpg.Pool, directory: Path, *, rebuild_index: bool = True) -> dict:
    directory = Path(directory)
    manifest = verify_pack(directory)

    async with pool.acquire() as conn:
        async with conn.transaction():
            await _copy_pages(conn, directory / PAGES_FILE)
            await _copy_passages(conn, directory / PASSAGES_FILE)
            await conn.execute("DROP TABLE IF EXISTS seed_written_pages")
        if rebuild_index:
            await rebuild_hnsw(pool)

    log.info("loaded pack %s: %s passages", manifest.name, manifest.passage_count)
    return {"pack": manifest.name, "passages": manifest.passage_count}


def _hash(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:32]
