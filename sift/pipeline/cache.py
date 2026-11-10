"""Redis caches: the 24 h query cache (with single-flight dedup), and a URL →
page cache so a page fetched live can be served again before the worker has
indexed it. All values are zlib-compressed JSON; every key has a TTL."""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

QUERY_PREFIX = "sift:qcache:"
PAGE_PREFIX = "sift:pcache:"
LOCK_PREFIX = "sift:qlock:"
HITS_KEY = "sift:cache:hits"
MISSES_KEY = "sift:cache:misses"


def query_cache_key(
    query: str,
    *,
    max_results: int,
    max_tokens: int | None,
    depth: str,
    include_domains: list[str],
    exclude_domains: list[str],
    fresh_only: bool,
    time_range: str | None,
    include_raw_content: bool,
    include_answer: bool,
) -> str:
    """A canonical key: lowercase, whitespace-collapsed, domain lists sorted,
    auth fields (there are none in the model) excluded."""
    norm = json.dumps(
        {
            "q": " ".join(query.lower().split()),
            "mr": max_results,
            "mt": max_tokens,
            "d": depth,
            "in": sorted(d.strip().lower() for d in include_domains),
            "ex": sorted(d.strip().lower() for d in exclude_domains),
            "fo": fresh_only,
            "tr": time_range,
            "rc": include_raw_content,
            "ans": include_answer,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return QUERY_PREFIX + hashlib.sha256(norm.encode()).hexdigest()


def page_cache_key(url_norm: str) -> str:
    return PAGE_PREFIX + hashlib.sha256(url_norm.encode()).hexdigest()


def _compress(payload: Any) -> str:
    # base64 keeps the zlib bytes safe on a decode_responses=True Redis client.
    raw = json.dumps(payload, default=str).encode()
    return base64.b64encode(zlib.compress(raw)).decode("ascii")


def _decompress(value: str) -> Any:
    return json.loads(zlib.decompress(base64.b64decode(value)))


class QueryCache:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def get(self, key: str) -> dict | None:
        try:
            raw = await self.redis.get(key)
        except RedisError:
            return None
        if raw is None:
            await self._count(MISSES_KEY)
            return None
        await self._count(HITS_KEY)
        try:
            return _decompress(raw)
        except (zlib.error, ValueError):
            return None

    async def set(self, key: str, payload: dict, ttl: int) -> None:
        try:
            await self.redis.set(key, _compress(payload), ex=ttl)
        except RedisError:
            pass

    async def acquire_lock(self, key: str, ttl: int = 10) -> bool:
        """Single-flight: True means we won the lock and should compute."""
        try:
            return bool(await self.redis.set(LOCK_PREFIX + key, "1", nx=True, ex=ttl))
        except RedisError:
            return True  # if Redis is down we can't coordinate; just compute

    async def release_lock(self, key: str) -> None:
        try:
            await self.redis.delete(LOCK_PREFIX + key)
        except RedisError:
            pass

    async def wait_for(self, key: str, wait_s: float = 5.0, poll: float = 0.05) -> dict | None:
        """Poll for another request's result while it holds the lock."""
        import asyncio

        deadline = asyncio.get_running_loop().time() + wait_s
        while asyncio.get_running_loop().time() < deadline:
            hit = await self.get(key)
            if hit is not None:
                return hit
            await asyncio.sleep(poll)
        return None

    async def _count(self, key: str) -> None:
        try:
            await self.redis.incr(key)
        except RedisError:
            pass

    async def stats(self) -> dict[str, int]:
        try:
            return {
                "hits": int(await self.redis.get(HITS_KEY) or 0),
                "misses": int(await self.redis.get(MISSES_KEY) or 0),
            }
        except RedisError:
            return {"hits": 0, "misses": 0}


class PageCache:
    def __init__(self, redis: Redis, ttl: int = 24 * 3600):
        self.redis = redis
        self.ttl = ttl

    async def get(self, url_norm: str) -> dict | None:
        try:
            raw = await self.redis.get(page_cache_key(url_norm))
        except RedisError:
            return None
        if raw is None:
            return None
        try:
            return _decompress(raw)
        except (zlib.error, ValueError):
            return None

    async def set(self, url_norm: str, page: dict) -> None:
        try:
            await self.redis.set(page_cache_key(url_norm), _compress(page), ex=self.ttl)
        except RedisError:
            pass
