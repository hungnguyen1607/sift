import httpx
import pytest
import respx

from sift.providers import (
    HackerNewsProvider,
    MarginaliaProvider,
    ProviderError,
    StackExchangeProvider,
    WikipediaProvider,
)


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@respx.mock
async def test_wikipedia(client):
    respx.get("https://en.wikipedia.org/w/api.php").respond(
        json={
            "query": {
                "search": [
                    {
                        "title": "PostgreSQL",
                        "snippet": '<span class="searchmatch">Postgre</span>SQL is a free &amp; open RDBMS',
                        "timestamp": "2026-11-01T18:44:32Z",
                    }
                ]
            }
        }
    )
    [c] = await WikipediaProvider(client).search("postgres", 5)
    assert c.url == "https://en.wikipedia.org/wiki/PostgreSQL"
    assert c.snippet == "Postgre SQL is a free & open RDBMS"
    assert c.updated.isoformat() == "2026-11-01T18:44:32+00:00"
    assert c.published is None


@respx.mock
async def test_stackexchange(client):
    route = respx.get("https://api.stackexchange.com/2.3/search/advanced").respond(
        json={
            "items": [
                {
                    "link": "https://stackoverflow.com/questions/1/q",
                    "title": "How to update &quot;jsonb&quot;?",
                    "body": "<p>I have a <code>jsonb</code> column</p>",
                    "creation_date": 1414957044,
                    "last_activity_date": 1740687929,
                    "score": 269,
                    "is_answered": True,
                    "tags": ["postgresql"],
                }
            ]
        }
    )
    p = StackExchangeProvider(client, key="k")
    [c] = await p.search("jsonb", 5)
    assert c.title == 'How to update "jsonb"?'
    assert c.snippet == "I have a jsonb column"
    assert c.published.year == 2014 and c.updated.year == 2025
    assert route.calls.last.request.url.params["key"] == "k"
    assert p.cap == 9_500 and p.period == "day"
    assert StackExchangeProvider(client).cap == 280


@respx.mock
async def test_hn_uses_discussion_url_for_ask_hn(client):
    respx.get("https://hn.algolia.com/api/v1/search").respond(
        json={
            "hits": [
                {
                    "objectID": "1",
                    "title": "Link post",
                    "url": "https://blog.example/x",
                    "created_at": "2015-12-21T10:00:00Z",
                    "points": 50,
                },
                {
                    "objectID": "2",
                    "title": "Ask HN: Postgres?",
                    "url": None,
                    "story_text": "<p>What do you use?</p>",
                },
                {"objectID": "3", "title": None},
            ]
        }
    )
    a, b = await HackerNewsProvider(client).search("postgres", 5)
    assert a.url == "https://blog.example/x"
    assert a.extra["discussion_url"] == "https://news.ycombinator.com/item?id=1"
    assert b.url == "https://news.ycombinator.com/item?id=2"
    assert b.snippet == "What do you use?"


@respx.mock
async def test_marginalia(client):
    route = respx.get("https://api2.marginalia-search.com/search").respond(
        json={"results": [{"url": "https://small.site/p", "title": "Small", "description": "d"}]}
    )
    [c] = await MarginaliaProvider(client, key="abc").search("q", 5)
    assert (c.url, c.title, c.snippet) == ("https://small.site/p", "Small", "d")
    assert route.calls.last.request.headers["API-Key"] == "abc"


@respx.mock
async def test_marginalia_rate_limit_text(client):
    respx.get("https://api2.marginalia-search.com/search").respond(200, text="QPM Limit Exceeded")
    with pytest.raises(ProviderError, match="rate limited"):
        await MarginaliaProvider(client).search("q", 5)


@respx.mock
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_http_errors_become_provider_errors(client, status):
    respx.get("https://hn.algolia.com/api/v1/search").respond(status)
    with pytest.raises(ProviderError):
        await HackerNewsProvider(client).search("q", 5)


@respx.mock
async def test_network_errors_become_provider_errors(client):
    respx.get("https://en.wikipedia.org/w/api.php").mock(side_effect=httpx.ConnectTimeout("t"))
    with pytest.raises(ProviderError, match="ConnectTimeout"):
        await WikipediaProvider(client).search("q", 5)


@respx.mock
async def test_hn_discussion_page_via_items_api(client):
    respx.get("https://hn.algolia.com/api/v1/items/42").respond(
        json={
            "title": "Ask HN: Postgres or MySQL?",
            "text": "<p>Which do you use for <i>JSON</i>?</p>",
            "created_at": "2024-01-02T03:04:05Z",
            "children": [
                {"author": "pg_fan", "text": "<p>Postgres, <code>jsonb</code> is great.</p>"},
                {"author": "ghost", "text": None},
            ],
        }
    )
    p = HackerNewsProvider(client)
    assert p.owns("https://news.ycombinator.com/item?id=42")
    assert not p.owns("https://example.com/item?id=42")
    page = await p.fetch_page("https://news.ycombinator.com/item?id=42")
    assert page.text == (
        "# Ask HN: Postgres or MySQL?\n\nWhich do you use for *JSON*?\n\n## Comments\n\n"
        "**pg_fan:** Postgres, `jsonb` is great."
    )
    assert page.published.year == 2024


@respx.mock
async def test_wikipedia_fetch_page(client):
    respx.get("https://en.wikipedia.org/w/api.php").respond(
        json={
            "query": {
                "pages": [
                    {
                        "title": "PostgreSQL",
                        "extract": "PostgreSQL is a DB.\n\n== History ==\nOld.",
                        "revisions": [{"timestamp": "2026-11-01T00:00:00Z"}],
                    }
                ]
            }
        }
    )
    p = WikipediaProvider(client)
    assert p.owns("https://en.wikipedia.org/wiki/PostgreSQL")
    page = await p.fetch_page("https://en.wikipedia.org/wiki/PostgreSQL")
    assert page.text == "PostgreSQL is a DB.\n\n## History\nOld."
    assert page.updated.isoformat() == "2026-11-01T00:00:00+00:00"


@respx.mock
async def test_stackexchange_content_has_question_and_top_answers(client):
    respx.get("https://api.stackexchange.com/2.3/search/advanced").respond(
        json={
            "items": [
                {
                    "link": "https://stackoverflow.com/questions/1/q",
                    "title": "Reverse a list?",
                    "body": "<p>How?</p>",
                    "answers": [
                        {"score": 5, "is_accepted": False, "body": "<p>low</p>"},
                        {
                            "score": 900,
                            "is_accepted": False,
                            "body": "<pre><code>xs[::-1]</code></pre>",
                        },
                        {"score": 1, "is_accepted": True, "body": "<p>accepted</p>"},
                        {"score": 50, "is_accepted": False, "body": "<p>mid</p>"},
                    ],
                }
            ]
        }
    )
    [c] = await StackExchangeProvider(client).search("reverse", 5)
    assert c.content == (
        "# Reverse a list?\n\nHow?\n\n## Accepted answer (score 1)\n\naccepted\n\n"
        "## Answer (score 900)\n\n```\nxs[::-1]\n```\n\n## Answer (score 50)\n\nmid"
    )


def test_parse_ts_returns_aware_datetime():
    from sift.providers.base import parse_ts

    assert parse_ts("2021-06-28").tzinfo is not None  # date-only → assumed UTC
    assert parse_ts("2021-06-28T00:00:00Z").tzinfo is not None
    assert parse_ts("2021-06-28T00:00:00").tzinfo is not None  # no zone → UTC
    assert parse_ts(None) is None
