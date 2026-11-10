import respx

from sift.pipeline.fetch import FetchResult
from sift.pipeline.models import Page
from sift.seeds.crawler import Crawler, extract_links, fetch_sitemap_urls, same_origin


def test_extract_links():
    html = '<a href="/b">b</a><a href="https://x.dev/c">c</a><a href="mailto:a@b.c">m</a>'
    links = extract_links(html, "https://a.dev/root")
    assert "https://a.dev/b" in links
    assert "https://x.dev/c" in links
    assert not any("mailto" in link for link in links)


def test_same_origin():
    assert same_origin("https://a.dev/docs/x", "https://a.dev/docs/")
    assert same_origin("https://a.dev/docs/x", "https://a.dev/docs")
    assert not same_origin("https://a.dev/other", "https://a.dev/docs")
    assert not same_origin("https://b.dev/docs", "https://a.dev/docs")


@respx.mock
async def test_fetch_sitemap_urlset():
    respx.get("https://a.dev/sitemap.xml").respond(
        200,
        text=(
            '<?xml version="1.0"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            "<url><loc>https://a.dev/1</loc></url>"
            "<url><loc>https://a.dev/2</loc></url>"
            "</urlset>"
        ),
    )
    import httpx

    async with httpx.AsyncClient() as c:
        assert await fetch_sitemap_urls(c, "https://a.dev/sitemap.xml") == [
            "https://a.dev/1",
            "https://a.dev/2",
        ]


async def test_crawler_follows_links():
    class FakeFetcher:
        def __init__(self, pages):
            self.pages = pages

        async def fetch(self, url):
            return self.pages[url]

    body_a = '<a href="/b">b</a><p>' + "hello world " * 40 + "</p>"
    body_b = "<p>" + "hello world " * 40 + "</p>"
    fetcher = FakeFetcher(
        {
            "https://a.dev/": FetchResult("https://a.dev/", body_a, "text/html", None),
            "https://a.dev/b": FetchResult("https://a.dev/b", body_b, "text/html", None),
        }
    )
    crawler = Crawler(fetcher, delay=0)

    async def fake_clean(html, url):
        return Page(url, "T", "hello world " * 40, "seed")

    crawler._clean = fake_clean  # avoid trafilatura in the test
    out = [p async for p in crawler.crawl(["https://a.dev/"], scope="https://a.dev/")]
    assert [p.url for p in out] == ["https://a.dev/", "https://a.dev/b"]


async def test_crawler_skips_non_html():
    class FakeFetcher:
        async def fetch(self, url):
            return FetchResult(url, "body", "application/pdf", None)

    crawler = Crawler(FakeFetcher(), delay=0)
    out = [p async for p in crawler.crawl(["https://a.dev/x.pdf"])]
    assert out == []
