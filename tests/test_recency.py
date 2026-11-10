import httpx
import pytest
import respx

from sift.providers import (
    BraveProvider,
    HackerNewsProvider,
    SerperProvider,
    StackExchangeProvider,
    TavilyProvider,
)


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_hn_adds_numeric_filters(client):
    route = respx.get("https://hn.algolia.com/api/v1/search").respond(json={"hits": []})
    await HackerNewsProvider(client).search("news", 5, time_range="week")
    params = route.calls.last.request.url.params
    assert params["numericFilters"].startswith("created_at_i>")


@respx.mock
async def test_stackexchange_adds_fromdate(client):
    route = respx.get("https://api.stackexchange.com/2.3/search/advanced").respond(
        json={"items": []}
    )
    await StackExchangeProvider(client).search("jsonb", 5, time_range="month")
    params = route.calls.last.request.url.params
    assert "fromdate" in params and int(params["fromdate"]) > 1_500_000_000


@respx.mock
async def test_brave_adds_freshness(client):
    route = respx.get("https://api.search.brave.com/res/v1/web/search").respond(
        json={"web": {"results": []}}
    )
    await BraveProvider(client, key="k").search("q", 5, time_range="week")
    assert route.calls.last.request.url.params["freshness"] == "pw"


@respx.mock
async def test_tavily_adds_time_range(client):
    route = respx.post("https://api.tavily.com/search").respond(json={"results": []})
    await TavilyProvider(client, key="k").search("q", 5, time_range="month")
    assert route.calls.last.request.content.decode().find('"time_range":"month"') != -1


@respx.mock
async def test_serper_adds_tbs(client):
    route = respx.post("https://google.serper.dev/search").respond(json={"organic": []})
    await SerperProvider(client, key="k").search("q", 5, time_range="day")
    assert route.calls.last.request.content.decode().find('"tbs":"qdr:d"') != -1


@respx.mock
async def test_no_time_range_means_no_recency_params(client):
    route = respx.get("https://hn.algolia.com/api/v1/search").respond(json={"hits": []})
    await HackerNewsProvider(client).search("news", 5)
    assert "numericFilters" not in route.calls.last.request.url.params
