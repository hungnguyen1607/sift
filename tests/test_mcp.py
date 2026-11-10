import httpx
import pytest
import respx

from sift.mcp_server.server import extract_impl, search_impl, verify_impl

BASE = "https://sift.test"


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE) as c:
        yield c


@respx.mock
async def test_search_impl_formats_compact_text(client):
    respx.post(f"{BASE}/v1/search").respond(
        json={
            "results": [
                {
                    "title": "Postgres JSONB",
                    "url": "https://pg.example/",
                    "content": "jsonb is binary",
                    "published": "2026-01-01",
                }
            ],
            "providers_used": ["wikipedia"],
            "cache_hit": False,
        }
    )
    out = await search_impl(client, "postgres jsonb")
    assert "[1] Postgres JSONB — https://pg.example/ (2026-01-01)" in out
    assert "> jsonb is binary" in out
    assert "providers: wikipedia" in out


@respx.mock
async def test_search_impl_passes_options(client):
    route = respx.post(f"{BASE}/v1/search").respond(json={"results": [], "providers_used": []})
    await search_impl(
        client, "q", max_tokens=500, depth="advanced", include_domains=["x.com"], fresh_only=True
    )
    body = route.calls.last.request.content.decode()
    assert '"max_tokens":500' in body
    assert '"search_depth":"advanced"' in body
    assert '"fresh_only":true' in body


@respx.mock
async def test_extract_impl_includes_failures(client):
    respx.post(f"{BASE}/v1/extract").respond(
        json={
            "results": [{"title": "T", "url": "https://a.dev/", "raw_content": "hello world"}],
            "failed_results": [{"url": "https://b.dev/", "error": "not a text document"}],
        }
    )
    out = await extract_impl(client, ["https://a.dev/", "https://b.dev/"])
    assert "# T (https://a.dev/)" in out and "hello world" in out
    assert "failed: https://b.dev/ — not a text document" in out


@respx.mock
async def test_verify_impl_reports_verdict(client):
    respx.post(f"{BASE}/v1/verify").respond(
        json={
            "verdict": "supported",
            "confidence": 0.85,
            "supporting": [{"quote": "x is y", "url": "https://a.dev/"}],
            "contradicting": [],
        }
    )
    out = await verify_impl(client, "x is y")
    assert out.startswith("Verdict: supported (confidence 0.85)")
    assert "'x is y' — https://a.dev/" in out
