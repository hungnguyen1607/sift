"""Engine cooldowns, request pacing, the stale-result fallback and /v1/health."""

import time
from contextlib import asynccontextmanager

import fakeredis
import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from sift.api.deps import Engine
from sift.api.main import create_app
from sift.api.ratelimit import RateLimiter
from sift.config import Settings
from sift.pipeline.cache import QueryCache
from sift.providers.base import Candidate, ProviderError, QuotaManager
from sift.providers.engine_health import EngineHealth
from sift.providers.searxng import SearXNGProvider

from .conftest import FakeEmbedder, FakeIndex, FakeProvider

SX = "http://searxng:8080/search"


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_blocks_cool_down_and_noise_is_ignored():
    clock = Clock()
    h = EngineHealth(cooldown_s=180, clock=clock)
    h.report([["brave", "too many requests"], ["wikidata", "timeout"], ["yahoo", "parsing error"]])
    h.report([["duckduckgo", "CAPTCHA"]])
    assert h.cooling() == ["brave", "duckduckgo"]
    assert h.disabled_param("general") == "brave__general,duckduckgo__general"
    assert h.disabled_param("videos, news") == (
        "brave__videos,brave__news,duckduckgo__videos,duckduckgo__news"
    )
    clock.t += 181
    assert h.cooling() == [] and h.disabled_param("general") is None


def test_repeat_blocks_cool_down_longer_then_reset():
    clock = Clock()
    h = EngineHealth(cooldown_s=100, max_cooldown_s=300, clock=clock)
    spans = []
    for _ in range(4):  # blocked again right after each cooldown ends
        h.report([["brave", "Suspended: too many requests"]])
        spans.append(h.snapshot()["brave"]["retry_in_s"])
        clock.t += spans[-1] + 1
    assert spans == [100, 200, 300, 300]
    clock.t += 1000  # a long healthy stretch resets the count
    h.report([["brave", "too many requests"]])
    assert h.snapshot()["brave"]["strikes"] == 1


@pytest.fixture
async def http():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_searxng_skips_cooling_engines(http):
    route = respx.get(SX).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "results": [{"url": "https://a.dev", "title": "a"}],
                    "unresponsive_engines": [["duckduckgo", "CAPTCHA"]],
                },
            ),
            httpx.Response(200, json={"results": [{"url": "https://b.dev", "title": "b"}]}),
        ]
    )
    sx = SearXNGProvider(http, "http://searxng:8080", min_gap_s=0)
    await sx.search_category("q", "general", 10)
    assert "disabled_engines" not in route.calls[0].request.url.params
    await sx.search_category("q", "videos", 10)
    assert route.calls[1].request.url.params["disabled_engines"] == "duckduckgo__videos"
    # Slow engines are cut off: general and vertical categories have their own caps.
    assert route.calls[0].request.url.params["timeout_limit"] == "2.0"
    assert route.calls[1].request.url.params["timeout_limit"] == "1.5"


@respx.mock
async def test_searxng_engine_timeout_override(http):
    route = respx.get("http://searxng:8080/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    sx = SearXNGProvider(http, "http://searxng:8080", min_gap_s=0)
    try:
        await sx.search_category("q", "general", 10, engine_timeout=1.2)
    except ProviderError:
        pass  # no results is fine here
    assert route.calls[0].request.url.params["timeout_limit"] == "1.2"


@respx.mock
async def test_searxng_empty_result_names_cooling_engines(http):
    health = EngineHealth()
    health.report([["brave", "too many requests"]])
    respx.get(SX).respond(json={"results": []})
    sx = SearXNGProvider(http, "http://searxng:8080", health=health, min_gap_s=0)
    with pytest.raises(Exception, match="brave \\(cooling down\\)"):
        await sx.search_category("q", "general", 10)


@respx.mock
async def test_searxng_requests_are_spaced(http):
    respx.get(SX).respond(json={"results": [{"url": "https://a.dev", "title": "a"}]})
    sx = SearXNGProvider(http, "http://searxng:8080", min_gap_s=0.05)
    t0 = time.monotonic()
    for _ in range(3):
        await sx.search_category("q", "general", 10)
    assert time.monotonic() - t0 >= 0.1


TEXT = "PostgreSQL jsonb is stored in a decomposed binary format that is faster to process."


@pytest.fixture
def app_client():
    wiki = FakeProvider(
        "wikipedia",
        [
            Candidate(
                "https://en.wikipedia.org/wiki/PostgreSQL",
                "PostgreSQL",
                "s",
                "wikipedia",
                content=TEXT,
            )
        ],
    )

    @asynccontextmanager
    async def lifespan(app):
        r = fakeredis.FakeAsyncRedis(decode_responses=True)
        quota = QuotaManager(r, {"wikipedia": (None, "month")})
        app.state.engine = Engine(
            Settings(), [wiki], quota, embedder=FakeEmbedder(), index=FakeIndex()
        )
        app.state.query_cache = QueryCache(r)
        app.state.rate_limiter = RateLimiter(r)
        app.state.redis = r
        yield

    with TestClient(create_app(lifespan)) as c:
        c.wiki = wiki
        yield c


def test_empty_live_search_serves_last_good_result(app_client):
    first = app_client.post("/v1/search", json={"query": "jsonb format"}).json()
    assert first["results"] and first["stale"] is False

    # The fresh cache expires, then every source comes back empty.
    async def drop_fresh():
        r = app_client.app.state.redis
        for k in await r.keys("sift:qcache:*"):
            await r.delete(k)

    app_client.portal.call(drop_fresh)
    app_client.wiki.results = []
    again = app_client.post("/v1/search", json={"query": "jsonb format"}).json()
    assert again["stale"] is True and again["stale_age_s"] is not None
    assert again["results"][0]["url"] == first["results"][0]["url"]


def test_empty_with_no_history_stays_empty(app_client):
    app_client.wiki.results = []
    body = app_client.post("/v1/search", json={"query": "never seen"}).json()
    assert body["results"] == [] and body["stale"] is False


def test_health_endpoint(app_client):
    body = app_client.get("/v1/health").json()
    assert body["searxng"] is False and body["providers"] == ["wikipedia"]
