from contextlib import asynccontextmanager

import fakeredis
import pytest
from fastapi.testclient import TestClient

from sift.api.deps import Engine
from sift.api.main import create_app
from sift.config import Settings
from sift.providers.base import Candidate, QuotaManager

from .conftest import FakeEmbedder, FakeIndex, FakeProvider

TEXT = (
    "# PostgreSQL JSON types\n\nPostgreSQL has json and jsonb. jsonb is stored in a decomposed "
    "binary format, which makes it slightly slower to input but significantly faster to process, "
    "since no reparsing is needed. jsonb also supports indexing, which can be a significant "
    "advantage.\n\n| type | stored as |\n|---|---|\n| json | text |\n| jsonb | binary |"
)


@pytest.fixture
def client():
    providers = [
        FakeProvider(
            "wikipedia",
            [
                Candidate(
                    "https://en.wikipedia.org/wiki/PostgreSQL",
                    "PostgreSQL",
                    "snippet",
                    "wikipedia",
                    content=TEXT,
                )
            ],
        ),
        FakeProvider(
            "hn",
            [
                Candidate("https://en.wikipedia.org/wiki/PostgreSQL", "PostgreSQL", "s", "hn"),
                Candidate("https://b.dev/x", "B", "unrelated cooking", "hn"),
            ],
        ),
    ]

    @asynccontextmanager
    async def lifespan(app):
        quota = QuotaManager(
            fakeredis.FakeAsyncRedis(decode_responses=True),
            {p.name: (None, "month") for p in providers},
        )
        app.state.engine = Engine(
            Settings(), providers, quota, embedder=FakeEmbedder(), index=FakeIndex()
        )
        yield

    with TestClient(create_app(lifespan)) as c:
        yield c


def test_search_returns_cited_passages(client):
    r = client.post(
        "/v1/search", json={"query": "jsonb binary format", "include_raw_content": True}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["providers_used"] == ["wikipedia", "hn"]
    assert body["cost_usd"] == 0 and body["cache_hit"] is False
    top = body["results"][0]
    assert top["url"] == "https://en.wikipedia.org/wiki/PostgreSQL"
    assert top["sources"] == ["wikipedia", "hn"]
    assert top["raw_content"][top["quote_start"] : top["quote_end"]] == top["content"]
    assert "| jsonb | binary |" in top["content"]
    assert 0 < top["score"] <= 1
    assert {"candidates", "fetch", "embed", "rank"} <= body["timings_ms"].keys()


def test_raw_content_omitted_by_default(client):
    top = client.post("/v1/search", json={"query": "jsonb"}).json()["results"][0]
    assert top["raw_content"] is None and top["quote_start"] is not None


def test_tavily_style_request_is_accepted(client):
    r = client.post(
        "/v1/search",
        json={
            "api_key": "tvly-x",
            "query": "postgres jsonb",
            "topic": "general",
            "max_results": 1,
            "search_depth": "advanced",
            "include_answer": False,
        },
    )
    assert r.status_code == 200
    assert len(r.json()["results"]) == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"query": ""}, {"query": "q", "max_results": 0}, {"query": "q", "search_depth": "deep"}],
)
def test_invalid_requests(client, payload):
    assert client.post("/v1/search", json=payload).status_code == 422


def test_usage(client):
    client.post("/v1/search", json={"query": "q"})
    assert client.get("/v1/usage").json()["providers"]["hn"]["used"] == 1
