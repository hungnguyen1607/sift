"""Seed-pack load integration tests. Run against a real Postgres when
SIFT_TEST_DATABASE_URL is set, e.g. inside the compose network."""

import os

import pytest

from sift.index.db import create_pool, migrate
from sift.index.search import LocalIndex
from sift.pipeline.embed import DIM
from sift.pipeline.models import Page
from sift.seeds.load import load_pack
from sift.seeds.packs import build_pack

DSN = os.environ.get("SIFT_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="SIFT_TEST_DATABASE_URL not set")


def _embed(texts):
    return [[0.25] * DIM for _ in texts]


@pytest.fixture
async def pool():
    p = await create_pool(DSN)
    await migrate(p)
    async with p.acquire() as c:
        await c.execute("TRUNCATE pages, passages, jobs, seed_packs RESTART IDENTITY CASCADE")
    yield p
    await p.close()


async def test_load_pack_and_search(tmp_path):
    pages = [
        Page(
            "https://pg.example/jsonb", "JSONB", "Postgres stores JSON as jsonb. " * 40, "devdocs"
        ),
        Page("https://pg.example/gin", "GIN", "GIN indexes speed up containment. " * 40, "devdocs"),
    ]
    build_pack(pages, tmp_path, name="devdocs", license="x", source="docs", embed=_embed)

    p = await create_pool(DSN)
    await migrate(p)
    try:
        result = await load_pack(p, tmp_path)
        assert result["passages"] > 0
        async with p.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM pages") == 2
            assert await c.fetchval("SELECT count(*) FROM passages") == result["passages"]
            assert await c.fetchval("SELECT count(*) FROM seed_packs") == 0  # CLI records it

        idx = LocalIndex(p)
        hits = await idx.search("jsonb", None, 5)
        assert hits and hits[0].page.origin == "index"
    finally:
        await p.close()


async def test_load_is_idempotent(tmp_path):
    pages = [Page("https://pg.example/jsonb", "JSONB", "Postgres jsonb. " * 40, "devdocs")]
    build_pack(pages, tmp_path, name="devdocs", license="x", source="docs", embed=_embed)

    p = await create_pool(DSN)
    await migrate(p)
    try:
        await load_pack(p, tmp_path)
        await load_pack(p, tmp_path)
        async with p.acquire() as c:
            assert await c.fetchval("SELECT count(*) FROM pages") == 1
    finally:
        await p.close()
