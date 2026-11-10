"""Runs against a real Postgres+pgvector when SIFT_TEST_DATABASE_URL is set,
e.g. inside the compose network. Skipped otherwise."""

import asyncio
import os

import pytest

from sift.index.db import create_pool, migrate
from sift.index.search import LocalIndex
from sift.pipeline.embed import DIM
from sift.worker import queue
from sift.worker.__main__ import run_once
from sift.worker.jobs import page_payload

from .conftest import FakeEmbedder

DSN = os.environ.get("SIFT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="SIFT_TEST_DATABASE_URL not set")

DOC = (
    "# GIN indexes\n\n"
    + "PostgreSQL GIN indexes make jsonb containment queries fast. " * 30
    + "\n\n## Operator classes\n\n"
    + "The jsonb_path_ops operator class builds smaller indexes for the @> operator. " * 30
)


@pytest.fixture
async def pool():
    p = await create_pool(DSN)
    await migrate(p)
    async with p.acquire() as c:
        await c.execute("TRUNCATE pages, passages, jobs RESTART IDENTITY CASCADE")
    yield p
    await p.close()


def page(url="https://example.com/gin", text=DOC):
    from sift.pipeline.models import Page

    return Page(url, "GIN indexes", text, "web")


async def test_worker_indexes_page_and_local_search_finds_it(pool):
    emb = FakeEmbedder()
    await queue.enqueue(pool, "index_page", page_payload(page()))
    assert await run_once(pool, emb) is True
    assert await run_once(pool, emb) is False  # queue drained

    async with pool.acquire() as c:
        n, dims, bad = await c.fetchrow(
            "SELECT count(*), max(vector_dims(embedding::vector)), "
            "count(*) FILTER (WHERE substr(pg.content, char_start + 1, char_end - char_start) <> ps.content) "
            "FROM passages ps JOIN pages pg ON pg.id = ps.page_id"
        )
        status = await c.fetchval("SELECT status FROM jobs")
    assert n >= 2 and dims == DIM and bad == 0 and status == "done"

    idx = LocalIndex(pool)
    hits = await idx.search(
        "jsonb_path_ops operator class", await emb.embed_query("jsonb_path_ops"), 5
    )
    assert hits and hits[0].heading == "Operator classes"
    assert hits[0].page.origin == "index"
    assert await idx.search("jsonb containment", None, 5)  # full-text only path

    # Known-page lookup normalizes URLs (www, trailing slash, utm).
    known = await idx.known_pages(["https://www.example.com/gin/?utm_source=x"])
    [(key, passages)] = known.items()
    texts = await idx.page_texts([passages[0].page.url])
    assert texts[key][passages[0].start : passages[0].end] == passages[0].text


async def test_unchanged_page_is_not_reembedded(pool):
    emb = FakeEmbedder()
    for _ in range(2):
        await queue.enqueue(pool, "index_page", page_payload(page()))
        await run_once(pool, emb)
    assert emb.passage_calls == 1
    await queue.enqueue(pool, "index_page", page_payload(page(text=DOC + "\n\nNew paragraph.")))
    await run_once(pool, emb)
    assert emb.passage_calls == 2
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT count(*) FROM pages") == 1


async def test_skip_locked_never_double_claims(pool):
    for i in range(20):
        await queue.enqueue(pool, "index_page", page_payload(page(url=f"https://e.com/{i}")))

    async def claimer():
        got = []
        async with pool.acquire() as c:
            while True:
                async with c.transaction():
                    job = await queue.claim(c)
                if job is None:
                    return got
                got.append(job.id)

    results = await asyncio.gather(*(claimer() for _ in range(4)))
    ids = [i for r in results for i in r]
    assert sorted(ids) == list(range(1, 21))


async def test_failing_job_retries_then_fails(pool):
    class Broken(FakeEmbedder):
        async def embed_passages(self, texts):
            raise RuntimeError("model crashed")

    await queue.enqueue(pool, "index_page", page_payload(page()))
    await run_once(pool, Broken())
    async with pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT status, attempts, last_error, run_after > now() AS later FROM jobs"
        )
        assert (row["status"], row["attempts"], row["later"]) == ("queued", 1, True)
        assert "model crashed" in row["last_error"]
        await c.execute("UPDATE jobs SET attempts = 2, run_after = now()")
    await run_once(pool, Broken())
    async with pool.acquire() as c:
        assert await c.fetchval("SELECT status FROM jobs") == "failed"


async def test_touch_and_evict_lru(pool):
    from sift.index.maintenance import evict_pages

    emb = FakeEmbedder()
    for i in range(5):
        await queue.enqueue(pool, "index_page", page_payload(page(url=f"https://e{i}.com/")))
        await run_once(pool, emb)

    idx = LocalIndex(pool)
    await idx.touch(["https://e0.com/"])  # make page 0 the most-recently-used

    async with pool.acquire() as c:
        deleted = await evict_pages(c, max_pages=2, retention_days=0)
        remaining = {r["url"] for r in await c.fetch("SELECT url FROM pages")}
    assert deleted == 3
    assert remaining == {"https://e0.com/", "https://e4.com/"}
