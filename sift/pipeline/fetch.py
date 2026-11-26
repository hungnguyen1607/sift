"""Polite page fetching: robots.txt on every redirect hop, an honest
User-Agent, a hard timeout and size cap, and caches for robots files and
failed URLs so Sift never hammers a site."""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = logging.getLogger(__name__)

ROBOTS_TOKEN = "Sift"
_HTML_TYPES = ("text/html", "application/xhtml+xml")
_TEXT_TYPES = ("text/plain", "text/markdown")
_SKIP_EXT = (
    ".pdf",
    ".zip",
    ".gz",
    ".tar",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".mp4",
    ".mp3",
    ".webm",
    ".exe",
    ".dmg",
    ".iso",
    ".woff",
    ".woff2",
)
_ROBOTS_OK_TTL = 24 * 3600
_ROBOTS_ERR_TTL = 3600
_FAIL_TTL = 3600
# Robots files are stored with a status prefix: "A" allow-all, "D" disallow-all,
# "R" followed by the robots.txt body.


class FetchError(Exception):
    def __init__(self, msg: str, *, retry_after: int | None = None):
        super().__init__(msg)
        self.retry_after = retry_after  # set when the site asked us to slow down


# A site that answers 429/503 is left alone for its Retry-After (clamped), not
# just the one URL: other pages on it would get the same answer.
_HOST_WAIT = "sift:hostwait:"
_HOST_WAIT_DEFAULT = 600
_HOST_WAIT_RANGE = (60, 3600)


def _retry_after(value: str | None) -> int:
    lo, hi = _HOST_WAIT_RANGE
    if not value:
        return _HOST_WAIT_DEFAULT
    value = value.strip()
    if value.isdigit():
        return max(lo, min(hi, int(value)))
    try:
        delta = parsedate_to_datetime(value).timestamp() - time.time()
    except (TypeError, ValueError):
        return _HOST_WAIT_DEFAULT
    return max(lo, min(hi, int(delta)))


@dataclass(slots=True)
class FetchResult:
    url: str  # final URL after redirects
    body: str
    content_type: str
    last_modified: object | None = None  # datetime

    @property
    def is_html(self) -> bool:
        return self.content_type in _HTML_TYPES


class RobotsCache:
    """robots.txt per origin, cached in-process and in Redis.

    Follows RFC 9309: a 4xx robots.txt means everything is allowed; a 5xx or an
    unreachable server means nothing is (we'd rather skip a page than be rude).
    """

    def __init__(self, client: httpx.AsyncClient, redis: Redis | None, timeout: float = 2.0):
        self.client = client
        self.redis = redis
        self.timeout = timeout
        self._mem: dict[str, tuple[float, RobotFileParser | bool]] = {}

    async def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        rules = await self._rules(origin)
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(ROBOTS_TOKEN, url)

    async def _rules(self, origin: str) -> RobotFileParser | bool:
        hit = self._mem.get(origin)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        raw = await self._redis_get(origin)
        ttl = _ROBOTS_OK_TTL
        if raw is None:
            raw, ttl = await self._download(origin)
            await self._redis_set(origin, raw, ttl)
        rules = self._parse(raw)
        self._mem[origin] = (time.monotonic() + min(ttl, 600), rules)
        if len(self._mem) > 5000:
            self._mem.clear()
        return rules

    async def _download(self, origin: str) -> tuple[str, int]:
        try:
            resp = await self.client.get(
                origin + "/robots.txt", timeout=self.timeout, follow_redirects=True
            )
        except httpx.HTTPError:
            return "D", _ROBOTS_ERR_TTL
        if resp.status_code >= 500:
            return "D", _ROBOTS_ERR_TTL
        if resp.status_code >= 400:
            return "A", _ROBOTS_OK_TTL
        return "R" + resp.text[:500_000], _ROBOTS_OK_TTL

    @staticmethod
    def _parse(raw: str) -> RobotFileParser | bool:
        if raw == "A":
            return True
        if raw == "D":
            return False
        rp = RobotFileParser()
        rp.parse(raw[1:].splitlines())
        return rp

    async def _redis_get(self, origin: str) -> str | None:
        if self.redis is None:
            return None
        try:
            return await self.redis.get(f"sift:robots:{origin}")
        except RedisError:
            return None

    async def _redis_set(self, origin: str, raw: str, ttl: int) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(f"sift:robots:{origin}", raw, ex=ttl)
        except RedisError:
            pass


class Fetcher:
    def __init__(
        self,
        client: httpx.AsyncClient,
        redis: Redis | None = None,
        *,
        timeout: float = 3.0,
        max_bytes: int = 3_000_000,
        max_redirects: int = 5,
    ):
        self.client = client
        self.redis = redis
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self.robots = RobotsCache(client, redis)

    @staticmethod
    def _fail_key(url: str) -> str:
        return "sift:fetchfail:" + hashlib.sha1(url.encode()).hexdigest()

    async def fetch(self, url: str, *, accept: tuple[str, ...] = ()) -> FetchResult:
        """``accept``: extra content types to allow besides HTML and text
        (e.g. a store's JSON product endpoint)."""
        if await self._recently_failed(url):
            raise FetchError("failed recently; skipping")
        host = urlsplit(url).netloc.lower()
        if await self._host_waiting(host):
            raise FetchError("site asked us to slow down; waiting")
        try:
            return await self._fetch(url, accept)
        except FetchError as e:
            await self._mark_failed(url, str(e))
            if e.retry_after is not None:  # 429/503: back off the whole site
                await self._wait_host(host, e.retry_after)
            raise

    async def _fetch(self, url: str, accept: tuple[str, ...] = ()) -> FetchResult:
        deadline = time.monotonic() + self.timeout
        for _ in range(self.max_redirects + 1):
            parts = urlsplit(url)
            if parts.scheme not in ("http", "https"):
                raise FetchError(f"unsupported scheme {parts.scheme!r}")
            if parts.path.lower().endswith(_SKIP_EXT):
                raise FetchError("not a text document")
            if not await self.robots.allowed(url):
                raise FetchError("disallowed by robots.txt")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FetchError("timeout")
            try:
                async with self.client.stream(
                    "GET",
                    url,
                    timeout=httpx.Timeout(remaining, connect=min(remaining, 1.5)),
                    follow_redirects=False,
                    headers={"Accept": "text/html,application/xhtml+xml,text/plain;q=0.8"},
                ) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("redirect without location")
                        url = urljoin(url, location)
                        continue
                    if resp.status_code in (429, 503):
                        raise FetchError(
                            f"HTTP {resp.status_code}",
                            retry_after=_retry_after(resp.headers.get("retry-after")),
                        )
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in _HTML_TYPES + _TEXT_TYPES + accept:
                        raise FetchError(f"unsupported content type {ctype or 'unknown'!r}")
                    declared = int(resp.headers.get("content-length") or 0)
                    if declared > self.max_bytes:
                        raise FetchError("page too large")
                    chunks, size = [], 0
                    async for part in resp.aiter_bytes():
                        size += len(part)
                        if size > self.max_bytes:
                            raise FetchError("page too large")
                        chunks.append(part)
                    body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
                    return FetchResult(url, body, ctype, _http_date(resp.headers))
            except httpx.TimeoutException as e:
                raise FetchError("timeout") from e
            except httpx.HTTPError as e:
                raise FetchError(f"{type(e).__name__}") from e
        raise FetchError("too many redirects")

    async def _host_waiting(self, host: str) -> bool:
        if self.redis is None or not host:
            return False
        try:
            return bool(await self.redis.exists(_HOST_WAIT + host))
        except RedisError:
            return False

    async def _wait_host(self, host: str, seconds: int) -> None:
        if self.redis is None or not host:
            return
        try:
            await self.redis.set(_HOST_WAIT + host, "1", ex=seconds)
        except RedisError:
            pass

    async def _recently_failed(self, url: str) -> bool:
        if self.redis is None:
            return False
        try:
            return bool(await self.redis.exists(self._fail_key(url)))
        except RedisError:
            return False

    async def _mark_failed(self, url: str, reason: str) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(self._fail_key(url), reason, ex=_FAIL_TTL)
        except RedisError:
            pass


def _http_date(headers: httpx.Headers):
    value = headers.get("last-modified")
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
