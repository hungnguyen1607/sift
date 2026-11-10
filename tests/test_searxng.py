import httpx
import pytest
import respx

from sift.providers import SearXNGProvider


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_searxng_search_maps_results(client):
    route = respx.get("http://searxng:8080/search").respond(
        json={
            "results": [
                {
                    "title": "Postgres JSONB",
                    "url": "https://pg.example/jsonb",
                    "content": "<b>jsonb</b> is a binary format",
                    "engine": "google",
                    "score": 0.9,
                },
                {"title": None, "url": "https://x.example/", "content": ""},
            ]
        }
    )
    p = SearXNGProvider(client, "http://searxng:8080")
    results = await p.search("postgres json", 5)
    assert results[0].url == "https://pg.example/jsonb"
    assert results[0].title == "Postgres JSONB"
    assert results[0].snippet == "jsonb is a binary format"  # tags stripped
    assert results[0].source == "searxng"
    assert results[1].title == "https://x.example/"  # falls back to url
    params = route.calls.last.request.url.params
    assert params["format"] == "json" and params["categories"] == "general"


@respx.mock
async def test_searxng_respects_limit(client):
    respx.get("http://searxng:8080/search").respond(
        json={
            "results": [
                {"title": str(i), "url": f"https://x.example/{i}", "content": "c"}
                for i in range(10)
            ]
        }
    )
    p = SearXNGProvider(client, "http://searxng:8080")
    assert len(await p.search("q", 3)) == 3
