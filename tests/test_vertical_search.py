import fakeredis

from sift.api.deps import Engine
from sift.config import Settings
from sift.pipeline.fetch import FetchError, FetchResult
from sift.pipeline.search import run_search
from sift.providers.base import Candidate, ProviderError, QuotaManager
from sift.providers.searxng import SearXNGProvider

from .conftest import FakeEmbedder, FakeIndex, FakeProvider


class FakeSearX(SearXNGProvider):
    """SearXNG with canned results per (categories, site-filtered?) source."""

    def __init__(self, by_category=None, *, error=None):
        super().__init__(None, "http://searxng")
        self.by_category = by_category or {}
        self.error = error
        self.queries: list[tuple[str, str]] = []

    async def search_category(
        self, query, categories, limit, *, page=1, time_range=None, http_timeout=None, **kw
    ):
        self.queries.append((query, categories))
        if self.error:
            raise self.error
        return list(self.by_category.get(categories, []))[:limit] if page == 1 else []


class NoFetch:
    async def fetch(self, url, accept=()):
        raise AssertionError(f"must not fetch {url}")


class PageFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    async def fetch(self, url, accept=()):
        self.fetched.append(url)
        if url not in self.pages:
            raise FetchError("disallowed by robots.txt")
        return FetchResult(url, self.pages[url], "text/html")


def engine(sx, providers=(), vertical_providers=None, fetcher=None):
    providers = list(providers)
    return Engine(
        settings=Settings(),
        providers=providers,
        fallbacks=[sx] if sx else [],
        vertical_providers=vertical_providers or {},
        quota=QuotaManager(fakeredis.FakeAsyncRedis(decode_responses=True), {}),
        embedder=FakeEmbedder(),
        index=FakeIndex(),
        fetcher=fetcher or NoFetch(),
    )


def c(url, title, snippet="", **extra):
    return Candidate(url, title, snippet, "searxng", extra=extra)


async def test_videos_come_from_the_video_category_deduped_with_fields():
    sx = FakeSearX(
        {
            "videos": [
                c("https://www.youtube.com/watch?v=a1&t=5", "Asyncio in Python", length="24:59"),
                c("https://youtu.be/a1", "Asyncio in Python (dup)"),
                c("https://vimeo.com/9", "Python asyncio talk", author="PyCon"),
            ]
        }
    )
    out = await run_search(engine(sx), "python asyncio tutorial video")
    assert out.vertical == "videos"
    urls = [r.passage.page.url for r in out.results]
    assert urls.count("https://www.youtube.com/watch?v=a1") == 1 and len(urls) == 2
    yt = next(r for r in out.results if "youtube" in r.passage.page.url).passage.page
    assert yt.fields["duration"] == "24:59" and yt.fields["platform"] == "youtube"
    assert sx.queries[0] == ("python asyncio tutorial", "videos")  # "video" dropped


async def test_people_rank_full_matches_first_and_report_shortfall():
    sx = FakeSearX(
        {
            "general": [
                c(
                    "https://ca.linkedin.com/in/dhruv",
                    "Dhruv - Web Developer | 2x Shopify Certified | LinkedIn",
                    "Education: Concordia University",
                ),
                c("https://www.linkedin.com/posts/x_activity", "A post about Shopify", "Concordia"),
                c(
                    "https://ca.linkedin.com/in/jane",
                    "Jane Doe - Software Engineer at Shopify | LinkedIn",
                    "Experience: Shopify · Education: Concordia University · Location: Montreal",
                ),
            ]
        }
    )
    out = await run_search(engine(sx), "find 5 Concordia grads at Shopify")
    assert out.vertical == "people"
    pages = [r.passage.page for r in out.results]
    assert [p.url for p in pages] == [
        "https://www.linkedin.com/in/jane",
        "https://www.linkedin.com/in/dhruv",
    ]  # the certified-only match ranks below; the post is dropped
    assert pages[0].title == "Jane Doe - Software Engineer at Shopify"  # " | LinkedIn" gone
    assert pages[0].fields["school"] == "Concordia University"
    assert out.providers_skipped["listing"] == "found 2 of 5"
    assert sx.queries[0] == ("Concordia Shopify site:linkedin.com/in", "general")


async def test_empty_auto_vertical_falls_back_to_web():
    # "remote python jobs" auto-detects jobs, but the job sources find nothing.
    # A search engine must still return something: retry as plain web.
    sx = FakeSearX({})  # jobs sites + categories all empty
    web = FakeProvider(
        "web",
        [
            c(f"https://ex.dev/{i}", "remote python jobs guide", "remote python jobs")
            for i in range(5)
        ],
    )
    e = engine(sx, providers=[web])
    out = await run_search(e, "remote python jobs montreal")
    assert out.vertical == "web"
    assert out.fell_back_to_web is True
    assert out.detected_vertical == "jobs"
    assert out.results and web.calls == 1


async def test_agent_forced_vertical_does_not_fall_back_to_web():
    sx = FakeSearX({})
    web = FakeProvider("web", [c("https://ex.dev/1", "jobs", "jobs")])
    e = engine(sx, providers=[web])
    out = await run_search(e, "remote python jobs", vertical="jobs")
    assert out.vertical == "jobs"
    assert out.fell_back_to_web is False
    assert out.results == [] and web.calls == 0


async def test_jobs_merge_feeds_and_read_json_ld_from_ats_pages():
    ld = (
        '<script type="application/ld+json">{"@type": "JobPosting", "title": "Backend Dev",'
        ' "hiringOrganization": {"name": "Acme"}, "datePosted": "2026-11-01"}</script>'
    )
    sx = FakeSearX(
        {
            "general": [
                c(
                    "https://jobs.lever.co/acme/123/apply",
                    "Acme - Backend Dev (Python)",
                    "Montreal",
                ),
                c("https://jobs.lever.co/acme", "Acme jobs"),  # a board, not a posting
            ]
        }
    )
    feed = FakeProvider(
        "arbeitnow",
        [
            Candidate(
                "https://arbeitnow.com/1",
                "Python Developer at B",
                "python montreal remote",
                "arbeitnow",
                extra={"job": {"company": "B", "remote": True}},
            )
        ],
    )
    fetcher = PageFetcher({"https://jobs.lever.co/acme/123": ld})
    e = engine(sx, vertical_providers={"arbeitnow": feed}, fetcher=fetcher)
    out = await run_search(e, "remote python jobs montreal")
    assert out.vertical == "jobs"
    by_url = {r.passage.page.url: r.passage.page for r in out.results}
    assert set(by_url) == {"https://jobs.lever.co/acme/123", "https://arbeitnow.com/1"}
    assert by_url["https://jobs.lever.co/acme/123"].fields["company"] == "Acme"
    assert by_url["https://arbeitnow.com/1"].fields["remote"] is True
    assert fetcher.fetched == ["https://jobs.lever.co/acme/123"]  # feed entries not fetched


async def test_shopping_reads_prices_and_applies_the_cap():
    def product(price):
        return (
            '<script type="application/ld+json">{"@type": "Product", "name": "K",'
            f' "offers": {{"price": "{price}", "priceCurrency": "USD"}}}}</script>'
        )

    sx = FakeSearX(
        {
            "general": [
                c("https://shop-a.example/k2", "Keychron K2 mechanical keyboard", "keyboard"),
                c("https://shop-b.example/pro", "Pro mechanical keyboard", "keyboard"),
                c("https://amazon.example/k", "Mechanical keyboard", "keyboard"),
            ]
        }
    )
    fetcher = PageFetcher(
        {
            "https://shop-a.example/k2": product("89.00"),
            "https://shop-b.example/pro": product("249"),
        }
    )
    out = await run_search(engine(sx, fetcher=fetcher), "mechanical keyboard under $100")
    assert out.vertical == "shopping"
    by_url = {r.passage.page.url: r.passage.page for r in out.results}
    assert "https://shop-b.example/pro" not in by_url  # $249 is over the cap
    assert by_url["https://shop-a.example/k2"].fields["price"] == 89.0
    assert "price_checked_at" in by_url["https://shop-a.example/k2"].fields
    assert "https://amazon.example/k" in by_url  # robots-blocked: kept, no price


async def test_shopping_listing_pages_become_priced_products():
    listing = (
        '<script type="application/ld+json">{"@type": "ItemList", "itemListElement": ['
        '{"@type": "ListItem", "position": 1, "item": {"@type": "Product", "name": "Board A",'
        ' "url": "/p/board-a", "offers": {"price": "79.99", "priceCurrency": "USD"}}},'
        '{"@type": "ListItem", "position": 2, "item": {"@type": "Product", "name": "Board B",'
        ' "url": "/p/board-b", "offers": {"price": "149.00", "priceCurrency": "USD"}}}'
        "]}</script>"
    )
    sx = FakeSearX(
        {
            "general": [
                c("https://store.example/c/keyboards", "Mechanical keyboards", "keyboards"),
                c("https://en.wikipedia.org/wiki/Keyboard", "Keyboard", "keyboard history"),
                c("https://cheap.example/kb", "Mechanical keyboard $59.99", "free shipping"),
            ]
        }
    )
    fetcher = PageFetcher({"https://store.example/c/keyboards": listing})
    out = await run_search(engine(sx, fetcher=fetcher), "buy mechanical keyboard under $100")
    by_url = {r.passage.page.url: r.passage.page.fields for r in out.results}
    a = by_url["https://store.example/p/board-a"]
    assert a["price"] == 79.99 and a["listed_on"] == "https://store.example/c/keyboards"
    assert "https://store.example/p/board-b" not in by_url  # over the cap
    assert by_url["https://cheap.example/kb"]["price_source"] == "snippet"
    assert "https://en.wikipedia.org/wiki/Keyboard" not in fetcher.fetched
    urls = [r.passage.page.url for r in out.results]
    assert urls.index("https://en.wikipedia.org/wiki/Keyboard") > urls.index(
        "https://cheap.example/kb"
    )
    # the extra "<item> price" search ran alongside the plain one
    assert ("mechanical keyboard price", "general") in sx.queries


async def test_shopping_snippet_amount_from_the_query_is_not_a_price():
    sx = FakeSearX(
        {"general": [c("https://shop.example/c", "Keyboards under $100", "best boards")]}
    )
    out = await run_search(engine(sx), "mechanical keyboard under $100")
    assert "price" not in (out.results[0].passage.page.fields or {})


async def test_places_come_from_the_map_category():
    sx = FakeSearX(
        {
            "map": [
                c(
                    "https://openstreetmap.org/node/1",
                    "Tim Hortons",
                    "",
                    type="coffeehouse",
                    latitude="45.49",
                    longitude="-73.57",
                )
            ]
        }
    )
    out = await run_search(engine(sx), "cafes near Concordia University")
    assert out.vertical == "places"
    [r] = out.results
    assert r.passage.page.fields == {"type": "coffeehouse", "lat": 45.49, "lon": -73.57}


async def test_news_uses_only_news_engines_and_drops_off_topic_entries():
    sx = FakeSearX(
        {
            "news": [
                c("https://npr.example/openai", "California sues OpenAI", "OpenAI case"),
                c("https://wikinews.example/pyramids", "Pyramids photo exhibition", "photos"),
            ]
        }
    )
    other = FakeProvider(
        "wikipedia", [Candidate("https://en.wikipedia.org/wiki/OpenAI", "OpenAI", "x", "wikipedia")]
    )
    fetcher = PageFetcher({"https://npr.example/openai": "<html><p>OpenAI was sued.</p></html>"})
    out = await run_search(engine(sx, [other], fetcher=fetcher), "latest openai news")
    assert out.vertical == "news"
    urls = {r.passage.page.url for r in out.results}
    assert urls == {"https://npr.example/openai"}
    assert other.calls == 0


async def test_unavailable_engines_are_reported_not_hidden():
    sx = FakeSearX(error=ProviderError("searxng: no results, engines unavailable: brave (429)"))
    out = await run_search(engine(sx), "python asyncio tutorial video")
    assert out.results == []
    assert "brave (429)" in out.providers_skipped["searxng:videos"]


async def test_answer_searches_stay_capped_at_20():
    out = await run_search(engine(None), "how do I reverse a list in python", max_results=80)
    assert out.vertical == "web" and len(out.results) <= 20


async def test_places_keep_the_main_cluster_in_source_order():
    def place(n, lat, lon):
        return c(f"https://openstreetmap.org/node/{n}", f"Cafe {n}", latitude=lat, longitude=lon)

    sx = FakeSearX(
        {
            "map": [
                place(1, "41.86", "-87.67"),  # Chicago
                place(2, "45.4958", "-73.5788"),
                place(3, "45.4957", "-73.5790"),
                place(4, "45.4954", "-73.5777"),
            ]
        }
    )
    out = await run_search(engine(sx), "cafes near Concordia University")
    assert [r.passage.page.title for r in out.results] == ["Cafe 2", "Cafe 3", "Cafe 4"]


async def test_a_named_platform_ranks_first_and_hn_links_to_the_thread():
    sx = FakeSearX(
        {
            "general": [
                c(
                    "https://www.reddit.com/r/framework/comments/1/review/",
                    "Framework laptop review",
                )
            ]
        }
    )
    hn = FakeProvider(
        "hn",
        [
            Candidate(
                "https://frame.work/blog/x",
                "The Framework Laptop",
                "framework laptop",
                "hn",
                extra={"discussion_url": "https://news.ycombinator.com/item?id=7"},
            )
        ],
    )
    out = await run_search(
        engine(sx, vertical_providers={"hn": hn}), "reddit posts about framework laptop"
    )
    urls = [r.passage.page.url for r in out.results]
    assert urls == [
        "https://www.reddit.com/r/framework/comments/1/review/",
        "https://news.ycombinator.com/item?id=7",
    ]
    assert out.results[1].passage.page.fields["platform"] == "hackernews"


async def test_named_platform_picks_the_site_even_though_its_name_is_stripped():
    sx = FakeSearX({"general": [c("https://www.instagram.com/mtlfood/", "MTL Food (@mtlfood)")]})
    out = await run_search(engine(sx), "montreal food creators on instagram")
    assert sx.queries[0] == ("montreal food creator site:instagram.com", "general")
    assert [r.passage.page.url for r in out.results] == ["https://instagram.com/mtlfood"]


async def test_site_search_drops_off_site_results_and_falls_back_to_a_keyed_provider():
    sx = FakeSearX(error=ProviderError("searxng: no results, engines unavailable: bing (429)"))
    brave = FakeProvider(
        "brave",
        [
            Candidate(
                "https://ca.linkedin.com/in/jane",
                "Jane - Engineer at Shopify",
                "Concordia",
                "brave",
            ),
            Candidate(
                "https://blog.example/linkedin-tips", "LinkedIn tips", "Shopify Concordia", "brave"
            ),
        ],
    )
    e = engine(sx)
    e.fallbacks.append(brave)
    out = await run_search(e, "find Concordia grads at Shopify")
    assert [r.passage.page.url for r in out.results] == ["https://www.linkedin.com/in/jane"]
    assert brave.calls == 1


async def test_latest_news_defaults_to_the_last_month():
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    new = c("https://npr.example/openai", "OpenAI sued", "OpenAI case")
    new.published = now - timedelta(days=1)
    old = c("https://wikinews.example/ai", "OpenAI and energy", "OpenAI energy")
    old.published = now - timedelta(days=700)
    sx = FakeSearX({"news": [old, new]})
    fetcher = PageFetcher(
        {
            "https://npr.example/openai": "<html><p>OpenAI was sued this week.</p></html>",
            "https://wikinews.example/ai": "<html><p>OpenAI and energy supply.</p></html>",
        }
    )
    out = await run_search(engine(sx, fetcher=fetcher), "latest openai news")
    assert [r.passage.page.url for r in out.results] == ["https://npr.example/openai"]
