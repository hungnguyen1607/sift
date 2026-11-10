from contextlib import ExitStack, asynccontextmanager

import fakeredis
import pytest
from fastapi.testclient import TestClient

from sift.api.auth import ApiKey, new_key
from sift.api.deps import Engine
from sift.api.main import create_app
from sift.api.ratelimit import RateLimiter
from sift.config import Settings, get_settings
from sift.pipeline.cache import QueryCache
from sift.providers.base import Candidate, QuotaManager

from .conftest import FakeEmbedder, FakeIndex, FakeProvider

CONTENT = "PostgreSQL stores JSON as jsonb " * 20


class FakeKeyStore:
    def __init__(self, valid=None, revoked=None):
        self.valid = valid or {}
        self.revoked = set(revoked or [])

    async def get(self, h):
        return self.valid.get(h)

    async def is_revoked(self, h):
        return h in self.revoked


def _key(rpm=60):
    full, prefix, digest = new_key()
    return full, prefix, digest, ApiKey(1, "demo", prefix, rpm, None)


@pytest.fixture
def make_client(monkeypatch):
    stack = ExitStack()

    def _make(auth="off", store=None, rpm=60):
        providers = [
            FakeProvider(
                "wikipedia",
                [
                    Candidate(
                        "https://en.wikipedia.org/wiki/X", "X", "s", "wikipedia", content=CONTENT
                    )
                ],
            )
        ]

        @asynccontextmanager
        async def lifespan(app):
            r = fakeredis.FakeAsyncRedis(decode_responses=True)
            quota = QuotaManager(r, {p.name: (None, "month") for p in providers})
            app.state.engine = Engine(
                Settings(), providers, quota, embedder=FakeEmbedder(), index=FakeIndex()
            )
            app.state.query_cache = QueryCache(r)
            app.state.rate_limiter = RateLimiter(r)
            app.state.key_store = store or FakeKeyStore()
            yield

        c = TestClient(create_app(lifespan))
        stack.enter_context(c)
        return c

    yield _make
    stack.close()


def _auth_on(monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("SIFT_AUTH", "on")


def test_auth_off_allows_anonymous(make_client):
    c = make_client("off")
    assert c.post("/v1/search", json={"query": "postgres jsonb"}).status_code == 200


def test_auth_on_requires_key(make_client, monkeypatch):
    _auth_on(monkeypatch)
    c = make_client("on")
    r = c.post("/v1/search", json={"query": "postgres jsonb"})
    assert r.status_code == 401


def test_auth_on_accepts_bearer_key(make_client, monkeypatch):
    _auth_on(monkeypatch)
    full, prefix, digest, key = _key()
    c = make_client("on", store=FakeKeyStore(valid={digest: key}))
    r = c.post(
        "/v1/search", json={"query": "postgres jsonb"}, headers={"Authorization": f"Bearer {full}"}
    )
    assert r.status_code == 200
    assert r.headers["X-RateLimit-Limit"] == "60"


def test_auth_on_accepts_body_api_key(make_client, monkeypatch):
    _auth_on(monkeypatch)
    full, prefix, digest, key = _key()
    c = make_client("on", store=FakeKeyStore(valid={digest: key}))
    r = c.post("/v1/search", json={"query": "postgres jsonb", "api_key": full})
    assert r.status_code == 200


def test_auth_on_rejects_unknown_key(make_client, monkeypatch):
    _auth_on(monkeypatch)
    c = make_client("on")
    r = c.post("/v1/search", json={"query": "q"}, headers={"Authorization": "Bearer sk_sift_bogus"})
    assert r.status_code == 401


def test_revoked_key_gets_403(make_client, monkeypatch):
    _auth_on(monkeypatch)
    full, prefix, digest, key = _key()
    store = FakeKeyStore(valid={}, revoked={digest})
    c = make_client("on", store=store)
    r = c.post("/v1/search", json={"query": "q"}, headers={"Authorization": f"Bearer {full}"})
    assert r.status_code == 403


def test_rate_limit_returns_429_and_headers(make_client, monkeypatch):
    _auth_on(monkeypatch)
    full, prefix, digest, key = _key(rpm=2)
    c = make_client("on", store=FakeKeyStore(valid={digest: key}))
    for _ in range(2):
        assert (
            c.post(
                "/v1/search",
                json={"query": "postgres jsonb"},
                headers={"Authorization": f"Bearer {full}"},
            ).status_code
            == 200
        )
    r = c.post(
        "/v1/search", json={"query": "postgres jsonb"}, headers={"Authorization": f"Bearer {full}"}
    )
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert r.headers["X-RateLimit-Remaining"] == "0"


async def test_rate_limiter_unit_sixty_per_minute(redis):
    rl = RateLimiter(redis)
    for _ in range(60):
        allowed, _ = await rl.check("k", 60)
        assert allowed
    allowed, info = await rl.check("k", 60)
    assert not allowed and info["remaining"] == 0
