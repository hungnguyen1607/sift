from sift.api.deps import Engine
from sift.config import Settings
from sift.pipeline.cache import PageCache
from sift.pipeline.extract import extract_urls
from sift.pipeline.fetch import FetchError, FetchResult
from sift.pipeline.models import Page
from sift.providers.base import QuotaManager

from .conftest import FakeIndex, FakeProvider, RecordingQueue


class OwnProvider(FakeProvider):
    def __init__(self, name, owned_url, page):
        super().__init__(name, [])
        self.owned_url = owned_url
        self.page = page

    def owns(self, url):
        return url == self.owned_url

    async def fetch_page(self, url):
        return self.page


class FakeFetcher:
    def __init__(self, pages=None, failures=None):
        self.pages = pages or {}
        self.failures = failures or {}

    async def fetch(self, url):
        if url in self.failures:
            raise self.failures[url]
        body, ctype = self.pages[url]
        return FetchResult(url, body, ctype, None)


def engine(providers, fetcher, index=None, cache=None):
    r = __import__("fakeredis").FakeAsyncRedis(decode_responses=True)
    quota = QuotaManager(r, {p.name: (p.cap, p.period) for p in providers})
    e = Engine(
        settings=Settings(),
        providers=providers,
        quota=quota,
        fetcher=fetcher,
        index=index or FakeIndex(),
        enqueue=RecordingQueue(),
        page_cache=cache or PageCache(r),
    )
    return e


async def test_extract_mixed_urls():
    wiki_page = Page(
        "https://en.wikipedia.org/wiki/PostgreSQL", "PostgreSQL", "pg text", "wikipedia"
    )
    so_page = Page("https://stackoverflow.com/q/1", "Q", "answer text", "stackexchange")
    providers = [
        OwnProvider("wikipedia", "https://en.wikipedia.org/wiki/PostgreSQL", wiki_page),
        OwnProvider("stackexchange", "https://stackoverflow.com/q/1", so_page),
    ]
    fetcher = FakeFetcher(
        pages={"https://blog.example/post": ("# Hello\n\nSome markdown body text.", "text/plain")},
        failures={
            "https://example.com/doc.pdf": FetchError("not a text document"),
            "https://blocked.example/": FetchError("disallowed by robots.txt"),
        },
    )
    e = engine(providers, fetcher)
    results, failed = await extract_urls(
        e,
        [
            "https://en.wikipedia.org/wiki/PostgreSQL",
            "https://stackoverflow.com/q/1",
            "https://blog.example/post",
            "https://example.com/doc.pdf",
            "https://blocked.example/",
        ],
    )
    by_url = {r["url"]: r for r in results}
    assert "https://en.wikipedia.org/wiki/PostgreSQL" in by_url
    assert "https://stackoverflow.com/q/1" in by_url
    assert by_url["https://blog.example/post"]["raw_content"].startswith("# Hello")
    failed_urls = {f["url"]: f["error"] for f in failed}
    assert failed_urls["https://example.com/doc.pdf"] == "not a text document"
    assert failed_urls["https://blocked.example/"] == "disallowed by robots.txt"


async def test_extract_uses_index_then_cache():
    indexed = {
        "https://a.dev/x": {"url": "https://a.dev/x", "title": "A", "raw_content": "from index"}
    }
    idx = FakeIndex(extractable=indexed)
    e = engine([], FakeFetcher(), index=idx)
    results, failed = await extract_urls(e, ["https://a.dev/x"])
    assert results[0]["raw_content"] == "from index" and not failed


async def test_extract_dedups_urls():
    fetcher = FakeFetcher(pages={"https://a.dev/x": ("body", "text/plain")})
    e = engine([], fetcher)
    results, failed = await extract_urls(e, ["https://a.dev/x", "https://a.dev/x"])
    assert len(results) == 1 and not failed


async def test_extract_provider_quota_exhausted_fails():
    providers = [OwnProvider("stackexchange", "https://stackoverflow.com/q/1", None)]
    providers[0].cap = 0
    e = engine(providers, FakeFetcher())
    results, failed = await extract_urls(e, ["https://stackoverflow.com/q/1"])
    assert not results and failed[0]["url"] == "https://stackoverflow.com/q/1"
