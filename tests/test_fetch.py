import fakeredis
import httpx
import pytest
import respx

from sift.pipeline.fetch import Fetcher, FetchError

HTML = {"content-type": "text/html; charset=utf-8"}


@pytest.fixture
async def fetcher():
    async with httpx.AsyncClient() as c:
        yield Fetcher(c, fakeredis.FakeAsyncRedis(decode_responses=True), timeout=2)


@respx.mock
async def test_fetch_ok_and_robots_cached(fetcher):
    robots = respx.get("https://a.com/robots.txt").respond(text="User-agent: *\nDisallow: /private")
    respx.get("https://a.com/page").respond(text="<p>hi</p>", headers=HTML)
    respx.get("https://a.com/other").respond(text="<p>yo</p>", headers=HTML)
    assert (await fetcher.fetch("https://a.com/page")).body == "<p>hi</p>"
    await fetcher.fetch("https://a.com/other")
    assert robots.call_count == 1


@respx.mock
async def test_robots_disallow(fetcher):
    respx.get("https://a.com/robots.txt").respond(text="User-agent: *\nDisallow: /private")
    page = respx.get("https://a.com/private/x").respond(text="secret", headers=HTML)
    with pytest.raises(FetchError, match="robots"):
        await fetcher.fetch("https://a.com/private/x")
    assert not page.called


@respx.mock
async def test_robots_targeting_sift(fetcher):
    respx.get("https://a.com/robots.txt").respond(text="User-agent: Sift\nDisallow: /\n")
    with pytest.raises(FetchError, match="robots"):
        await fetcher.fetch("https://a.com/x")


@respx.mock
async def test_robots_404_allows_and_5xx_disallows(fetcher):
    respx.get("https://a.com/robots.txt").respond(404)
    respx.get("https://a.com/x").respond(text="ok", headers=HTML)
    respx.get("https://b.com/robots.txt").respond(503)
    b = respx.get("https://b.com/x").respond(text="ok", headers=HTML)
    assert (await fetcher.fetch("https://a.com/x")).body == "ok"
    with pytest.raises(FetchError, match="robots"):
        await fetcher.fetch("https://b.com/x")
    assert not b.called


@respx.mock
async def test_redirect_into_disallowed_site_is_blocked(fetcher):
    respx.get("https://a.com/robots.txt").respond(404)
    respx.get("https://b.com/robots.txt").respond(text="User-agent: *\nDisallow: /")
    respx.get("https://a.com/go").respond(302, headers={"location": "https://b.com/landing"})
    landing = respx.get("https://b.com/landing").respond(text="x", headers=HTML)
    with pytest.raises(FetchError, match="robots"):
        await fetcher.fetch("https://a.com/go")
    assert not landing.called


@respx.mock
async def test_rejects_binary_and_oversized(fetcher):
    respx.get("https://a.com/robots.txt").respond(404)
    respx.get("https://a.com/img").respond(
        content=b"\x89PNG", headers={"content-type": "image/png"}
    )
    respx.get("https://a.com/big").respond(text="x" * 4_000_000, headers=HTML)
    with pytest.raises(FetchError, match="content type"):
        await fetcher.fetch("https://a.com/img")
    with pytest.raises(FetchError, match="too large"):
        await fetcher.fetch("https://a.com/big")
    with pytest.raises(FetchError, match="not a text"):
        await fetcher.fetch("https://a.com/paper.pdf")


@respx.mock
async def test_failures_are_cached(fetcher):
    respx.get("https://a.com/robots.txt").respond(404)
    page = respx.get("https://a.com/x").respond(500)
    for _ in range(3):
        with pytest.raises(FetchError):
            await fetcher.fetch("https://a.com/x")
    assert page.call_count == 1


@respx.mock
async def test_timeout_is_a_fetch_error(fetcher):
    respx.get("https://a.com/robots.txt").respond(404)
    respx.get("https://a.com/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError, match="timeout"):
        await fetcher.fetch("https://a.com/slow")
