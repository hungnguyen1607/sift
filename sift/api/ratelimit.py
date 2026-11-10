"""Per-key rate limiting: a Redis sliding window (two fixed windows weighted),
plus the X-RateLimit-* response headers."""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Response
from redis.asyncio import Redis
from redis.exceptions import RedisError

from sift.api.auth import ApiKey, ApiKeyDep
from sift.config import get_settings


class RateLimiter:
    def __init__(self, redis: Redis, window: float = 60.0):
        self.redis = redis
        self.window = window

    async def check(self, scope: str, limit: int) -> tuple[bool, dict]:
        now = time.time()
        cur = int(now // self.window)
        cur_key = f"sift:rl:{scope}:{cur}"
        prev_key = f"sift:rl:{scope}:{cur - 1}"
        elapsed = now - cur * self.window
        try:
            pipe = self.redis.pipeline()
            pipe.incr(cur_key)
            pipe.expire(cur_key, int(self.window * 2))
            pipe.get(prev_key)
            current, _, prev = await pipe.execute()
            current, prev = int(current), int(prev or 0)
        except RedisError:
            return True, {"limit": limit, "remaining": limit, "reset": int(now + self.window)}
        weighted = prev * (1 - elapsed / self.window) + current
        remaining = max(limit - int(weighted), 0)
        reset = (cur + 1) * self.window
        return weighted <= limit, {"limit": limit, "remaining": remaining, "reset": int(reset)}


async def enforce_rate_limit(request: Request, response: Response, key: ApiKeyDep) -> ApiKey | None:
    settings = get_settings()
    limiter: RateLimiter = request.app.state.rate_limiter
    if key is not None:
        scope, limit = f"key:{key.id}", key.rpm_limit
    else:
        host = request.client.host if request.client else "unknown"
        scope, limit = f"ip:{host}", settings.rpm_limit
    allowed, info = await limiter.check(scope, limit)
    headers = {
        "X-RateLimit-Limit": str(limit),
        "X-RateLimit-Remaining": str(info["remaining"]),
        "X-RateLimit-Reset": str(info["reset"]),
    }
    response.headers.update(headers)
    if not allowed:
        retry_after = max(int(info["reset"] - time.time()), 1)
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded",
            headers={**headers, "Retry-After": str(retry_after)},
        )
    return key


RateLimitDep = Annotated[ApiKey | None, Depends(enforce_rate_limit)]
