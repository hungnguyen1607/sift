import respx

from examples.sift_search import search_sift

BASE = "http://localhost:8000"


@respx.mock
def test_search_sift_formats_citable_passages():
    respx.post(f"{BASE}/v1/search").respond(
        json={
            "results": [
                {
                    "title": "Postgres JSONB",
                    "url": "https://pg.example/",
                    "content": "jsonb is binary",
                }
            ]
        }
    )
    out = search_sift("postgres jsonb", base_url=BASE)
    assert "[1] Postgres JSONB — https://pg.example/" in out
    assert "> jsonb is binary" in out


@respx.mock
def test_search_sift_sends_auth():
    route = respx.post(f"{BASE}/v1/search").respond(json={"results": []})
    search_sift("q", base_url=BASE, api_key="sk_sift_x")
    assert route.calls.last.request.headers["Authorization"] == "Bearer sk_sift_x"
