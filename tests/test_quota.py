import asyncio
from datetime import UTC, datetime

from redis.exceptions import ConnectionError as RedisConnectionError

from sift.pipeline.candidates import gather_candidates
from sift.providers.base import QuotaManager

from .conftest import FakeProvider, cand


async def test_cap_is_never_exceeded_sequentially(redis):
    q = QuotaManager(redis, {"brave": (3, "month")})
    results = [await q.try_acquire("brave") for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert (await q.usage())["brave"] == {"used": 3, "cap": 3, "period": "month", "remaining": 0}


async def test_cap_is_never_exceeded_under_concurrency(redis):
    q = QuotaManager(redis, {"brave": (10, "month")})
    results = await asyncio.gather(*(q.try_acquire("brave") for _ in range(200)))
    assert sum(results) == 10
    assert (await q.usage())["brave"]["used"] == 10


async def test_counters_reset_each_period(redis):
    q = QuotaManager(redis, {"se": (1, "day"), "brave": (1, "month")})
    d1, d2 = datetime(2026, 11, 8, tzinfo=UTC), datetime(2026, 11, 9, tzinfo=UTC)
    assert await q.try_acquire("se", d1) and not await q.try_acquire("se", d1)
    assert await q.try_acquire("se", d2)
    assert await q.try_acquire("brave", d1) and not await q.try_acquire("brave", d2)
    assert await q.try_acquire("brave", datetime(2026, 12, 1, tzinfo=UTC))


async def test_uncapped_providers_are_counted_not_limited(redis):
    q = QuotaManager(redis, {"wikipedia": (None, "month")})
    assert all([await q.try_acquire("wikipedia") for _ in range(50)])
    assert (await q.usage())["wikipedia"]["used"] == 50


class BrokenRedis:
    async def incr(self, key):
        raise RedisConnectionError("down")

    async def get(self, key):
        raise RedisConnectionError("down")


async def test_redis_outage_fails_closed_for_capped_providers():
    q = QuotaManager(BrokenRedis(), {"brave": (1000, "month"), "hn": (None, "month")})
    assert await q.try_acquire("brave") is False
    assert await q.try_acquire("hn") is True


async def test_searches_skip_provider_once_cap_is_hit(quota_for):
    """The plan's quota test: cap Brave at 3, run 5 searches, 4-5 skip Brave."""
    brave = FakeProvider("brave", [cand("https://a.com")], cap=3)
    wiki = FakeProvider("wikipedia", [cand("https://en.wikipedia.org/wiki/A")])
    quota = quota_for([brave, wiki])
    runs = [await gather_candidates("q", [brave, wiki], quota) for _ in range(5)]
    assert [("brave" in r.providers_used) for r in runs] == [True, True, True, False, False]
    assert runs[4].providers_skipped["brave"] == "quota exhausted"
    assert brave.calls == 3 and wiki.calls == 5
