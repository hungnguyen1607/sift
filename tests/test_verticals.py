import json

import httpx
import pytest
import respx

from sift.pipeline.structured import job_fields, product_fields
from sift.pipeline.verticals import (
    VERTICALS,
    coverage,
    detect_vertical,
    job_canonical,
    job_url_ok,
    key_terms,
    news_fields,
    people_fields,
    pick_sites,
    place_fields,
    price_cap,
    profile_canonical,
    profile_url_ok,
    requested_count,
    search_query,
    social_url_ok,
    video_canonical,
    video_fields,
)
from sift.providers import ArbeitnowProvider, HNJobsProvider, SearXNGProvider
from sift.providers.base import Candidate
from sift.providers.wikivoyage import destination


@pytest.mark.parametrize(
    "query,vertical",
    [
        ("how do I reverse a list in python", "web"),
        ("python 3.12 features", "web"),
        ("python asyncio tutorial video", "videos"),
        ("youtube api quota", "web"),
        ("how to compress a video with ffmpeg", "web"),
        ("latest openai news", "news"),
        ("find 20 Concordia grads at Shopify", "people"),
        ("linkedin profiles of Concordia alumni working at Shopify", "people"),
        ("engineers at shopify salary", "web"),
        ("reddit posts about framework laptop", "social"),
        ("what do people think about the framework laptop", "social"),
        ("remote python jobs montreal", "jobs"),
        ("cron jobs in kubernetes", "web"),
        ("steve jobs biography", "web"),
        ("mechanical keyboard under $100", "shopping"),
        ("cafes near Concordia University", "places"),
        ("things to do in Lisbon", "travel"),
        ("5 days in lisbon itinerary", "travel"),
        ("arxiv paper on diffusion models", "academic"),
        ("site:linkedin.com/in Concordia Shopify", "people"),
        ("site:linkedin.com/jobs python", "jobs"),
        ("site:instagram.com montreal food creators", "people"),
        ("site:reddit.com framework laptop", "social"),
        ("site:youtube.com asyncio", "videos"),
        ("site:github.com rust tui", "site"),
    ],
)
def test_detect_vertical(query, vertical):
    assert detect_vertical(query) == vertical


@pytest.mark.parametrize(
    "query,n",
    [
        ("find 20 Concordia grads at Shopify", 20),
        ("list 25 remote python jobs", 25),
        ("30 software engineers at shopify", 30),
        ("python 3.12 features", None),
        ("5 days in lisbon", None),
        ("find 500 people", None),
    ],
)
def test_requested_count(query, n):
    assert requested_count(query) == n


def test_profile_urls_and_canonical_form():
    assert profile_url_ok("https://ca.linkedin.com/in/jane-doe")
    assert not profile_url_ok("https://www.linkedin.com/posts/jane-doe_activity-1")
    assert not profile_url_ok("https://www.linkedin.com/company/shopify")
    assert profile_url_ok("https://www.instagram.com/montrealfood/")
    assert not profile_url_ok("https://www.instagram.com/p/C123/")
    assert not profile_url_ok("https://github.com/rust-lang/rust")
    assert profile_canonical("https://ca.linkedin.com/in/Jane-Doe/") == profile_canonical(
        "https://www.linkedin.com/in/jane-doe"
    )


def test_video_canonical_merges_youtube_forms():
    want = "https://www.youtube.com/watch?v=abc123"
    assert video_canonical("https://youtu.be/abc123") == want
    assert video_canonical("https://m.youtube.com/watch?v=abc123&t=30s") == want
    assert video_canonical("https://www.youtube.com/shorts/abc123") == want
    assert video_canonical("https://vimeo.com/42") == "https://vimeo.com/42"


def test_social_and_job_urls():
    assert social_url_ok("https://www.reddit.com/r/framework/comments/1/x/")
    assert not social_url_ok("https://www.reddit.com/")
    assert social_url_ok("https://x.com/someone/status/1")
    assert not social_url_ok("https://x.com/someone")
    assert job_url_ok("https://jobs.lever.co/acme/1234-abcd")
    assert not job_url_ok("https://jobs.lever.co/acme")
    assert job_canonical("https://jobs.lever.co/acme/1234/apply?src=x") == (
        "https://jobs.lever.co/acme/1234"
    )


def test_pick_sites_follows_named_platforms():
    people = VERTICALS["people"]
    assert pick_sites(people, "Concordia grads at Shopify") == ("linkedin.com/in",)
    assert pick_sites(people, "food creators on instagram") == ("instagram.com",)
    assert pick_sites(VERTICALS["jobs"], "python jobs on linkedin") == ("linkedin.com/jobs",)


def test_search_query_and_key_terms_drop_vertical_words():
    people = VERTICALS["people"]
    assert search_query(people, "find 20 Concordia grads at Shopify") == "Concordia Shopify"
    assert search_query(people, "site:linkedin.com/in Concordia Shopify") == (
        "Concordia Shopify site:linkedin.com/in"
    )
    assert key_terms(people, "Concordia master's student at Shopify") == [
        "concordia",
        "master",
        "shopify",
    ]
    assert search_query(VERTICALS["news"], "latest openai news") == "openai"


def test_coverage_ignores_past_or_second_hand_ties():
    people, terms = VERTICALS["people"], ["concordia", "shopify"]
    assert coverage(people, terms, "Engineer at Shopify · Education: Concordia") == 1.0
    assert coverage(people, terms, "Dev @ Plusgrade | Shopify Certified · Concordia") == 0.5
    assert coverage(people, terms, "PM | Ex-Shopify · Concordia University") == 0.5
    assert coverage(people, terms, "Ex-Shopify, now at Shopify again · Concordia") == 1.0


def test_price_cap():
    assert price_cap("mechanical keyboard under $100") == 100.0
    assert price_cap("mechanical keyboard") is None


def test_field_parsers():
    li = Candidate(
        "https://ca.linkedin.com/in/jane",
        "Jane Doe - Software Engineer at Shopify | LinkedIn",
        "Experience: Shopify · Education: Concordia University · Location: Montreal · "
        "500+ connections on LinkedIn.",
        "searxng",
    )
    f = people_fields(li)
    assert f["name"] == "Jane Doe" and f["headline"] == "Software Engineer at Shopify"
    assert f["company"] == "Shopify" and f["school"] == "Concordia University"
    assert f["location"] == "Montreal" and f["connections"] == "500+"

    ig = Candidate(
        "https://www.instagram.com/mtlfood/",
        "MTL Food (@mtlfood) • Instagram photos and videos",
        "12.5K Followers, 300 Following, 900 Posts",
        "searxng",
    )
    assert people_fields(ig) == {
        "platform": "instagram",
        "name": "MTL Food",
        "handle": "mtlfood",
        "followers": "12.5K",
    }

    vid = Candidate(
        "https://www.youtube.com/watch?v=1",
        "Asyncio",
        "",
        "searxng",
        extra={"length": "24:59", "author": "Tech With Tim"},
    )
    assert video_fields(vid) == {
        "platform": "youtube",
        "channel": "Tech With Tim",
        "duration": "24:59",
    }

    news = Candidate(
        "https://www.npr.org/x", "t", "", "searxng", extra={"metadata": "4 hours ago | NPR"}
    )
    assert news_fields(news) == {"publisher": "NPR", "age": "4 hours ago"}

    place = Candidate(
        "https://openstreetmap.org/node/1",
        "Tim Hortons",
        "",
        "searxng",
        extra={
            "type": "coffeehouse",
            "address": "{'house_number': '1550', 'road': 'Boulevard De Maisonneuve', "
            "'locality': 'Montreal', 'postcode': 'H3G'}",
            "latitude": "45.4958492",
            "longitude": "-73.5786315",
            "data": "[{'label': 'cuisine', 'key': 'cuisine', 'value': 'coffee_shop'}]",
            "links": "[{'label': 'official website', 'url': 'https://timhortons.ca'}]",
        },
    )
    f = place_fields(place)
    assert f["address"] == "1550 Boulevard De Maisonneuve, Montreal, H3G"
    assert f["lat"] == 45.4958492 and f["cuisine"] == "coffee_shop"
    assert f["website"] == "https://timhortons.ca"


def _ld(obj) -> str:
    return f'<html><script type="application/ld+json">{json.dumps(obj)}</script></html>'


def test_product_json_ld():
    html = _ld(
        {
            "@context": "https://schema.org",
            "@graph": [
                {"@type": "WebPage"},
                {
                    "@type": "Product",
                    "name": "Keychron K2",
                    "brand": {"@type": "Brand", "name": "Keychron"},
                    "offers": [
                        {
                            "@type": "Offer",
                            "price": "89.00",
                            "priceCurrency": "USD",
                            "availability": "https://schema.org/InStock",
                        }
                    ],
                    "aggregateRating": {"ratingValue": "4.6", "reviewCount": 120},
                },
            ],
        }
    )
    assert product_fields(html) == {
        "name": "Keychron K2",
        "brand": "Keychron",
        "price": 89.0,
        "currency": "USD",
        "availability": "InStock",
        "rating": 4.6,
        "reviews": 120.0,
    }
    meta = '<meta property="product:price:amount" content="19.99">'
    assert product_fields(meta) == {"price": 19.99}


def test_job_posting_json_ld():
    html = _ld(
        {
            "@type": "JobPosting",
            "title": "Backend Engineer",
            "hiringOrganization": {"name": "Acme"},
            "jobLocation": {"address": {"addressLocality": "Montreal", "addressCountry": "CA"}},
            "jobLocationType": "TELECOMMUTE",
            "baseSalary": {
                "currency": "CAD",
                "value": {"minValue": 120000, "maxValue": 150000, "unitText": "YEAR"},
            },
            "datePosted": "2026-11-01",
        }
    )
    f = job_fields(html)
    assert f["title"] == "Backend Engineer" and f["company"] == "Acme"
    assert f["location"] == "Montreal, CA" and f["remote"] is True
    assert f["salary"] == "120000–150000 CAD / YEAR"
    assert job_fields("<html>no data</html>") == {}


def test_wikivoyage_destination():
    assert destination("things to do in Lisbon") == "Lisbon"
    assert destination("where to stay in New York City") == "New York City"
    assert destination("cheap flights to Tokyo") == "Tokyo"


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_searxng_pages_until_enough_and_reads_extra_fields(client):
    def page(n):
        return [
            {"url": f"https://v.example/{n}-{i}", "title": "t", "length": "1:00"} for i in range(10)
        ]

    route = respx.get("http://searxng:8080/search").mock(
        side_effect=lambda req: httpx.Response(
            200, json={"results": page(int(req.url.params.get("pageno", 1)))}
        )
    )
    sx = SearXNGProvider(client, "http://searxng:8080")
    got = await sx.search_pages("asyncio", "videos", 25, max_pages=5)
    assert len(got) == 30 and route.call_count == 3
    assert got[0].extra["length"] == "1:00"
    assert route.calls[0].request.url.params["categories"] == "videos"


@respx.mock
async def test_searxng_reports_unavailable_engines(client):
    respx.get("http://searxng:8080/search").respond(
        json={"results": [], "unresponsive_engines": [["brave", "too many requests"]]}
    )
    sx = SearXNGProvider(client, "http://searxng:8080")
    with pytest.raises(Exception, match="brave \\(too many requests\\)"):
        await sx.search_category("x", "general", 10)


@respx.mock
async def test_arbeitnow_filters_locally_and_caches(client):
    route = respx.get(ArbeitnowProvider.URL).respond(
        json={
            "data": [
                {
                    "title": "Python Developer",
                    "company_name": "A",
                    "location": "Berlin",
                    "remote": True,
                    "url": "https://arbeitnow.com/1",
                    "tags": ["python"],
                },
                {
                    "title": "Java Developer",
                    "company_name": "B",
                    "location": "Berlin",
                    "remote": True,
                    "url": "https://arbeitnow.com/2",
                    "tags": [],
                },
                {
                    "title": "Python Developer",
                    "company_name": "C",
                    "location": "Munich",
                    "remote": False,
                    "url": "https://arbeitnow.com/3",
                    "tags": [],
                },
            ],
            "links": {"next": None},
        }
    )
    p = ArbeitnowProvider(client)
    got = await p.search("remote python jobs", 10)
    assert [c.url for c in got] == ["https://arbeitnow.com/1"]
    assert got[0].extra["job"]["remote"] is True
    await p.search("java", 10)
    assert route.call_count == 1  # cached feed


@respx.mock
async def test_hn_jobs_reads_header_lines(client):
    respx.get("https://hn.algolia.com/api/v1/search_by_date").respond(
        json={"hits": [{"objectID": "100", "title": "Ask HN: Who is hiring? (November 2026)"}]}
    )
    respx.get("https://hn.algolia.com/api/v1/search").respond(
        json={
            "hits": [
                {
                    "objectID": "1",
                    "parent_id": 100,
                    "comment_text": "Acme | Backend Engineer | Montreal | REMOTE<p>We use Python.",
                },
                {"objectID": "2", "parent_id": 1, "comment_text": "a reply"},
            ]
        }
    )
    got = await HNJobsProvider(client).search("python jobs", 10)
    assert len(got) == 1
    assert got[0].title == "Acme | Backend Engineer | Montreal | REMOTE"
    assert got[0].extra["job"]["company"] == "Acme" and got[0].extra["job"]["remote"] is True


def test_linkedin_name_from_slug_when_the_title_has_none():
    c = Candidate(
        "https://ca.linkedin.com/in/sarah-donovan-515366146",
        "User Researcher - Product Experience at Shopify | LinkedIn",
        "Experience: Shopify · Location: H3H 1A2. View Sarah's profile",
        "searxng",
    )
    f = people_fields(c)
    assert f["name"] == "Sarah Donovan"
    assert f["headline"] == "User Researcher - Product Experience at Shopify"
    assert f["location"] == "H3H 1A2"
    lepine = Candidate(
        "https://ca.linkedin.com/in/nick-lepine-a73", "Nick Lépine - PM @ Shopify", "", "s"
    )
    assert people_fields(lepine)["name"] == "Nick Lépine"  # accents still match the slug


def test_product_url():
    from sift.pipeline.verticals import product_url

    assert product_url("https://kbdfans.com/products/tofu65")
    assert product_url("https://kbdfans.com/collections/keyboards/products/tofu65")
    assert product_url("https://www.amazon.com/Keychron-K2/dp/B07QBPDWLS")
    assert not product_url("https://www.amazon.com/mechanical-keyboard/s?k=mechanical+keyboard")
    assert not product_url("https://unikeyboards.com/collections/mechanical-keyboards")


def test_product_group_price_comes_from_the_first_variant():
    html = _ld(
        {
            "@type": "ProductGroup",
            "name": "Keychron K2 Pro",
            "brand": {"name": "Keychron"},
            "hasVariant": [
                {"@type": "Product", "offers": {"price": "109.00", "priceCurrency": "USD"}},
                {"@type": "Product", "offers": {"price": "119.00", "priceCurrency": "USD"}},
            ],
        }
    )
    f = product_fields(html)
    assert f["price"] == 109.0 and f["currency"] == "USD" and f["brand"] == "Keychron"


def test_github_query_translation():
    from sift.providers.github_users import to_github_query

    assert to_github_query("github profiles of rust developers in berlin")[0] == (
        "location:berlin language:rust"
    )
    assert to_github_query("find 10 python developers in montreal on github")[0] == (
        "location:montreal language:python"
    )
    assert to_github_query("machine learning engineers in san francisco github")[0] == (
        'machine learning location:"san francisco"'
    )


@respx.mock
async def test_github_users_without_a_token(client):
    from sift.providers import GitHubUsersProvider

    route = respx.get("https://api.github.com/search/users").respond(
        json={
            "items": [
                {
                    "login": "spacejam",
                    "html_url": "https://github.com/spacejam",
                    "url": "https://api.github.com/users/spacejam",
                    "type": "User",
                }
            ]
        }
    )
    [c] = await GitHubUsersProvider(client).search("rust developers in berlin", 5)
    assert c.url == "https://github.com/spacejam" and c.title == "spacejam"
    assert c.extra["person"]["location"] == "berlin" and c.extra["person"]["language"] == "rust"
    assert route.calls.last.request.url.params["q"] == "location:berlin language:rust"


def test_looks_academic_handles_plurals():
    from sift.pipeline.keywords import looks_academic

    assert looks_academic("papers about vision transformers")


def test_people_search_uses_singular_roles_and_github_names():
    people = VERTICALS["people"]
    assert search_query(people, "instagram fitness influencers in miami") == (
        "fitness influencer miami"
    )
    assert search_query(people, "find fitness coaches in miami") == "fitness coach miami"
    gh = Candidate(
        "https://github.com/jonasgrunert", "Jonas Grunert jonasgrunert · GitHub", "", "s"
    )
    assert people_fields(gh)["name"] == "Jonas Grunert"


@pytest.mark.parametrize(
    "url,title,name",
    [
        ("https://github.com/byron", "Sebastian Thiel (Byron)", "Sebastian Thiel"),
        ("https://github.com/orhun", "orhun (Orhun Parmaksız)", "Orhun Parmaksız"),
        ("https://github.com/8192K", "8192K - Overview", "8192K"),
    ],
)
def test_github_title_forms(url, title, name):
    assert people_fields(Candidate(url, title, "", "s"))["name"] == name


def test_degree_and_title_spellings_count_as_the_same_term():
    v = VERTICALS["people"]
    terms = key_terms(v, "software engineers with a master from concordia at shopify")
    hay = "Jane Doe - Software Developer - Shopify. Education: Concordia University, M.Sc."
    assert coverage(v, terms, hay) == 1.0
    assert coverage(v, ["phd", "mila"], "Ph.D. student, Mila") == 1.0
    # A synonym never rescues a past tie.
    assert coverage(v, ["shopify"], "Developer, Ex-Shopify") == 0.0


@pytest.mark.parametrize(
    "url,title,want",
    [
        (
            "https://job-boards.greenhouse.io/later/jobs/1",
            "Job Application for Research Analyst at Later",
            {"title": "Research Analyst", "company": "Later"},
        ),
        (
            "https://job-boards.greenhouse.io/berlinpackaging/jobs/1",
            "Job Application for Sr. Azure DevOps and SRE Engineer at ...",
            {"title": "Sr. Azure DevOps and SRE Engineer", "company": "Berlinpackaging"},
        ),
        (
            "https://jobs.lever.co/kabam/abc",
            "Kabam - Data Analyst - jobs.lever.co",
            {"title": "Data Analyst", "company": "Kabam"},
        ),
        (  # the part before " - " isn't the company: keep the whole title
            "https://jobs.lever.co/smart-working-solutions/abc",
            "Senior Python Engineer - Data (Remote)",
            {
                "title": "Senior Python Engineer - Data (Remote)",
                "company": "Smart Working Solutions",
                "remote": True,
            },
        ),
        (
            "https://www.linkedin.com/jobs/view/123",
            "Shopify hiring Backend Developer in Montreal, QC | LinkedIn",
            {"title": "Backend Developer", "company": "Shopify", "location": "Montreal, QC"},
        ),
        (
            "https://jobs.ashbyhq.com/linear/1",
            "Product Engineer @ Linear",
            {"title": "Product Engineer", "company": "Linear"},
        ),
    ],
)
def test_job_fields_from_result_titles(url, title, want):
    from sift.pipeline.verticals import job_fields_from_title

    got = job_fields_from_title(Candidate(url, title, "", "searxng"))
    assert {k: got.get(k) for k in want} == want


def test_shop_canonical_merges_product_endpoints():
    from sift.pipeline.verticals import shop_canonical

    a = shop_canonical("https://www.keychron.com/products/k2.oembed")
    b = shop_canonical("https://www.keychron.com/products/k2?variant=123")
    assert a == b == "https://www.keychron.com/products/k2"
    assert shop_canonical("https://x.com/s?k=desk") == "https://x.com/s?k=desk"


def test_shopify_json_product():
    from sift.pipeline.structured import shopify_fields, shopify_json_url

    assert (
        shopify_json_url("https://www.keychron.com/en-ca/collections/all/products/k2-v2?variant=1")
        == "https://www.keychron.com/products/k2-v2.js"
    )
    assert shopify_json_url("https://www.amazon.com/dp/B07") is None
    body = '{"title": "K2", "vendor": "Keychron", "price": 4999, "price_min": 4999, "available": false, "variants": []}'
    assert shopify_fields(body) == {
        "name": "K2",
        "brand": "Keychron",
        "price": 49.99,
        "availability": "OutOfStock",
    }
    assert shopify_fields('{"not": "shopify"}') == {}


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("Keychron K2 $89.99 free shipping", (89.99, "USD")),
        ("From $129.00", (129.0, "USD")),
        ("CA$89.99 in stock", (89.99, "CAD")),
        ("$79 – $99", (79.0, "USD")),
        ("£45 at Argos", (45.0, "GBP")),
        ("Price: 1,299.00 USD", (1299.0, "USD")),
        ("Was $120 now $99", (99.0, "USD")),
        ("Save $20 on desks", None),
        ("free shipping over $35", None),
        ("$20 off orders", None),
        ("4.5 stars, 2,345 reviews", None),
    ],
)
def test_snippet_price(text, want):
    from sift.pipeline.verticals import snippet_price

    assert snippet_price(text) == want


def test_shop_fields_skips_pages_that_sell_nothing():
    from sift.pipeline.verticals import shop_fields

    wiki = Candidate("https://en.wikipedia.org/wiki/Desk", "Desk", "desks cost $200", "x")
    store = Candidate("https://desks.example/p/1", "Desk", "only $200", "x")
    assert "price" not in shop_fields(wiki)
    assert shop_fields(store)["price"] == 200.0


def test_price_query():
    from sift.pipeline.verticals import price_query

    assert price_query("for standing desk") == "standing desk price"
    assert price_query("keychron k2 prices") == "keychron k2 prices"


def test_product_list_reads_item_lists_and_product_arrays():
    from sift.pipeline.structured import product_list

    item_list = (
        '<script type="application/ld+json">{"@type": "ItemList", "itemListElement": ['
        '{"@type": "ListItem", "item": {"@type": "Product", "name": "A", "url": "/p/a",'
        ' "offers": {"price": "10", "priceCurrency": "USD"}}},'
        '{"@type": "ListItem", "item": {"@type": "Product", "name": "B", "url": "/p/b",'
        ' "offers": {"@type": "AggregateOffer", "lowPrice": 7}}}]}</script>'
    )
    got = product_list(item_list, "https://s.example/c/x")
    assert [(p["url"], p["price"]) for p in got] == [
        ("https://s.example/p/a", 10.0),
        ("https://s.example/p/b", 7.0),
    ]
    array = (
        '<script type="application/ld+json">[{"@type": "Product", "name": "C",'
        ' "offers": {"url": "https://s.example/p/c", "price": 5}},'
        ' {"@type": "Product", "name": "D", "url": "https://s.example/p/d"}]</script>'
    )
    assert [p["url"] for p in product_list(array, "https://s.example/")] == [
        "https://s.example/p/c",
        "https://s.example/p/d",
    ]
    # a listing page has no single product of its own
    assert product_fields(item_list) == {}


def test_shopify_collection_feed():
    from sift.pipeline.structured import shopify_collection, shopify_collection_url

    assert (
        shopify_collection_url("https://kb.example/en-ca/collections/keyboards?page=2")
        == "https://kb.example/collections/keyboards/products.json?limit=8"
    )
    assert shopify_collection_url("https://kb.example/products/k2") is None
    body = json.dumps(
        {
            "products": [
                {
                    "handle": "k2",
                    "title": "K2",
                    "vendor": "Keychron",
                    "variants": [
                        {"price": "99.00", "available": False},
                        {"price": "89.00", "available": True},
                    ],
                },
                {"handle": "no-title"},
            ]
        }
    )
    assert shopify_collection(body, "https://kb.example/collections/keyboards") == [
        {
            "url": "https://kb.example/products/k2",
            "name": "K2",
            "brand": "Keychron",
            "price": 89.0,
            "availability": "InStock",
        }
    ]


@pytest.mark.parametrize(
    ("url", "article"),
    [
        ("https://www.pcmag.com/picks/the-best-noise-cancelling-headphones", True),
        ("https://www.cnet.com/reviews/bose-700-review/", True),
        ("https://www.btod.com/blog/standing-desk-cost/", True),
        ("https://www.techradar.com/audio/headphones/best-noise-cancelling-headphones", True),
        ("https://www.bose.com/p/headphones/qc-ultra", False),
        ("https://www.bestbuy.com/site/headphones/noise-cancelling/pcmcat15.c", False),
        ("https://mechanicalkeyboards.com/collections/keyboard-sales", False),
    ],
)
def test_article_like(url, article):
    from sift.pipeline.verticals import article_like

    assert article_like(url) is article
