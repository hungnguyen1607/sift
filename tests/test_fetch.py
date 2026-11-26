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


@respx.mock
async def test_429_backs_off_the_whole_site(fetcher):
    respx.get("https://shop.com/robots.txt").respond(404)
    first = respx.get("https://shop.com/products/a").respond(429, headers={"Retry-After": "120"})
    other = respx.get("https://shop.com/products/b").respond(text="<p>b</p>", headers=HTML)
    respx.get("https://elsewhere.com/robots.txt").respond(404)
    respx.get("https://elsewhere.com/x").respond(text="<p>x</p>", headers=HTML)
    with pytest.raises(FetchError, match="429"):
        await fetcher.fetch("https://shop.com/products/a")
    with pytest.raises(FetchError, match="slow down"):
        await fetcher.fetch("https://shop.com/products/b")
    assert first.call_count == 1 and not other.called
    assert 0 < await fetcher.redis.ttl("sift:hostwait:shop.com") <= 120
    await fetcher.fetch("https://elsewhere.com/x")  # other sites are unaffected


def test_retry_after_parsing():
    from sift.pipeline.fetch import _retry_after

    assert _retry_after("120") == 120
    assert _retry_after("5") == 60  # clamped
    assert _retry_after("999999") == 3600
    assert _retry_after(None) == 600
    assert _retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 60  # in the past
    assert _retry_after("soon") == 600
