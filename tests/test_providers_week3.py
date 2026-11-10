import httpx
import pytest
import respx

from sift.providers import (
    ArxivProvider,
    BraveProvider,
    GitHubProvider,
    OpenAlexProvider,
    SerperProvider,
    TavilyProvider,
)
from sift.providers.openalex import rebuild_abstract


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_github_search(client):
    route = respx.get("https://api.github.com/search/repositories").respond(
        json={
            "items": [
                {
                    "html_url": "https://github.com/psf/requests",
                    "full_name": "psf/requests",
                    "description": "Python HTTP for Humans.",
                    "created_at": "2011-02-13T00:00:00Z",
                    "pushed_at": "2026-01-01T00:00:00Z",
                    "stargazers_count": 52000,
                    "language": "Python",
                }
            ]
        }
    )
    p = GitHubProvider(client, token="tok")
    [c] = await p.search("http client", 5)
    assert c.url == "https://github.com/psf/requests"
    assert c.title == "psf/requests"
    assert c.snippet == "Python HTTP for Humans."
    assert route.calls.last.request.headers["Authorization"] == "Bearer tok"
    assert p.cap == 30 and p.period == "minute"


@respx.mock
async def test_github_readme_via_raw_api(client):
    respx.get("https://api.github.com/repos/psf/requests/readme").respond(
        200, text="# requests\n\n```python\nimport requests\n```"
    )
    p = GitHubProvider(client, token="tok")
    assert p.owns("https://github.com/psf/requests")
    assert not p.owns("https://github.com/psf")
    page = await p.fetch_page("https://github.com/psf/requests")
    assert page.text.startswith("# psf/requests README")
    assert "```python" in page.text


def test_rebuild_abstract():
    inv = {"The": [0], "quick": [1], "brown": [2], "fox": [3]}
    assert rebuild_abstract(inv) == "The quick brown fox"
    assert rebuild_abstract(None) == ""


@respx.mock
async def test_openalex(client):
    respx.get("https://api.openalex.org/works").respond(
        json={
            "results": [
                {
                    "id": "https://openalex.org/W123",
                    "title": "A study of JSON databases",
                    "display_name": "A study of JSON databases",
                    "abstract_inverted_index": {"JSON": [0], "is": [1], "fast": [2]},
                    "publication_date": "2024-03-01",
                    "cited_by_count": 12,
                }
            ]
        }
    )
    p = OpenAlexProvider(client, mailto="me@example.com")
    [c] = await p.search("json database study", 5)
    assert c.url == "https://openalex.org/W123"
    assert c.snippet == "JSON is fast"
    assert c.content == "JSON is fast"
    assert p.academic_only is True


@respx.mock
async def test_brave(client):
    route = respx.get("https://api.search.brave.com/res/v1/web/search").respond(
        json={
            "web": {
                "results": [
                    {"title": "Postgres JSON", "url": "https://db.example/pg", "description": "d"}
                ]
            }
        }
    )
    p = BraveProvider(client, key="k")
    [c] = await p.search("postgres json", 5)
    assert (c.url, c.title) == ("https://db.example/pg", "Postgres JSON")
    assert route.calls.last.request.headers["X-Subscription-Token"] == "k"
    assert p.cap == 900 and p.period == "month" and p.rate_limit == (1, 1)


@respx.mock
async def test_tavily(client):
    route = respx.post("https://api.tavily.com/search").respond(
        json={"results": [{"title": "T", "url": "https://x.example", "content": "body"}]}
    )
    p = TavilyProvider(client, key="k")
    [c] = await p.search("q", 5)
    assert c.snippet == "body" and c.url == "https://x.example"
    assert route.calls.last.request.headers["Authorization"] == "Bearer k"


@respx.mock
async def test_serper(client):
    route = respx.post("https://google.serper.dev/search").respond(
        json={"organic": [{"title": "G", "link": "https://g.example", "snippet": "s"}]}
    )
    p = SerperProvider(client, key="k")
    [c] = await p.search("q", 5)
    assert c.url == "https://g.example" and c.snippet == "s"
    assert route.calls.last.request.headers["X-API-KEY"] == "k"
    assert p.period == "lifetime" and p.cap == 2400


@respx.mock
async def test_arxiv_atom(client):
    respx.get("https://export.arxiv.org/api/query").respond(
        200,
        text=(
            '<?xml version="1.0"?>'
            '<feed xmlns="http://www.w3.org/2005/Atom">'
            "<entry>"
            "<title>A Theorem</title>"
            "<summary>We prove a theorem.</summary>"
            "<id>https://arxiv.org/abs/2501.00001</id>"
            "<published>2025-01-01T00:00:00Z</published>"
            "<updated>2025-01-02T00:00:00Z</updated>"
            "</entry>"
            "</feed>"
        ),
    )
    [c] = await ArxivProvider(client).search("theorem", 5)
    assert c.url == "https://arxiv.org/abs/2501.00001"
    assert c.snippet == "We prove a theorem."
    assert c.content == "We prove a theorem."
    assert c.published.year == 2025
