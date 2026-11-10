from sift.pipeline.cache import PageCache, QueryCache, query_cache_key


def _key(query, **overrides):
    opts = dict(
        max_results=5,
        max_tokens=None,
        depth="basic",
        include_domains=[],
        exclude_domains=[],
        fresh_only=False,
        time_range=None,
        include_raw_content=False,
        include_answer=False,
    )
    opts.update(overrides)
    return query_cache_key(query, **opts)


def test_query_cache_key_normalizes():
    a = _key(
        "  PostgreSQL   JSONB ",
        include_domains=["B.com", "a.com"],
    )
    b = _key("postgresql jsonb", include_domains=["a.com", "b.com"])
    assert a == b


def test_query_cache_key_differs_on_options():
    a = _key("q")
    b = _key("q", depth="advanced")
    c = _key("q", fresh_only=True)
    assert a != b and a != c


async def test_query_cache_roundtrip(redis):
    cache = QueryCache(redis)
    key = _key("q")
    assert await cache.get(key) is None
    await cache.set(key, {"results": [1], "cache_hit": False}, 3600)
    hit = await cache.get(key)
    assert hit == {"results": [1], "cache_hit": False}


async def test_query_cache_stats(redis):
    cache = QueryCache(redis)
    key = _key("q")
    await cache.get(key)  # miss
    await cache.set(key, {"x": 1}, 3600)
    await cache.get(key)  # hit
    stats = await cache.stats()
    assert stats["hits"] == 1 and stats["misses"] == 1


async def test_single_flight_lock(redis):
    cache = QueryCache(redis)
    key = _key("q")
    assert await cache.acquire_lock(key) is True
    assert await cache.acquire_lock(key) is False
    await cache.release_lock(key)
    assert await cache.acquire_lock(key) is True


async def test_wait_for_returns_other_result(redis):
    cache = QueryCache(redis)
    key = _key("q")

    async def later():
        import asyncio

        await asyncio.sleep(0.05)
        await cache.set(key, {"result": "ok"}, 3600)

    import asyncio

    task = asyncio.create_task(later())
    got = await cache.wait_for(key, wait_s=2.0, poll=0.01)
    await task
    assert got == {"result": "ok"}


async def test_page_cache_roundtrip(redis):
    cache = PageCache(redis)
    assert await cache.get("https://a.dev/x") is None
    await cache.set("https://a.dev/x", {"url": "https://a.dev/x", "title": "T", "raw_content": "c"})
    assert (await cache.get("https://a.dev/x"))["raw_content"] == "c"
