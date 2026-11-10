from datetime import UTC, datetime

from sift.pipeline.candidates import gather_candidates
from sift.providers.base import QuotaManager

from .conftest import FakeProvider, cand


async def test_lifetime_period_never_resets(redis):
    q = QuotaManager(redis, {"serper": (2, "lifetime")})
    d1, d2 = datetime(2026, 1, 1, tzinfo=UTC), datetime(2027, 1, 1, tzinfo=UTC)
    assert await q.try_acquire("serper", d1) and await q.try_acquire("serper", d1)
    assert not await q.try_acquire("serper", d2)  # same lifetime key


async def test_minute_period_resets(redis):
    q = QuotaManager(redis, {"github": (1, "minute")})
    m1, m2 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC), datetime(2026, 1, 1, 10, 1, tzinfo=UTC)
    assert await q.try_acquire("github", m1)
    assert not await q.try_acquire("github", m1)
    assert await q.try_acquire("github", m2)


async def test_rate_limit_burst_smoothing(redis):
    q = QuotaManager(redis, {"brave": (900, "month")})
    assert await q.try_rate("brave", 1, 1)
    assert not await q.try_rate("brave", 1, 1)


async def test_academic_providers_skipped_for_non_academic_queries(quota_for):
    arxiv = FakeProvider("arxiv", [cand("https://arxiv.org/abs/1")])
    arxiv.academic_only = True
    wiki = FakeProvider("wikipedia", [cand("https://en.wikipedia.org/wiki/A")])
    quota = quota_for([arxiv, wiki])
    r = await gather_candidates("how do I make pasta", [arxiv, wiki], quota)
    assert r.providers_skipped["arxiv"] == "not academic"
    assert "arxiv" not in r.providers_used and "wikipedia" in r.providers_used


async def test_academic_providers_used_for_academic_queries(quota_for):
    arxiv = FakeProvider("arxiv", [cand("https://arxiv.org/abs/1")])
    arxiv.academic_only = True
    wiki = FakeProvider("wikipedia", [cand("https://en.wikipedia.org/wiki/A")])
    quota = quota_for([arxiv, wiki])
    r = await gather_candidates("a paper about the no free lunch theorem", [arxiv, wiki], quota)
    assert "arxiv" in r.providers_used


async def test_thin_rule_escalates_to_fallback(quota_for):
    base = FakeProvider("wikipedia", [cand("https://en.wikipedia.org/wiki/A")])
    fallback = FakeProvider("tavily", [cand("https://t.example/x")], cap=100)
    quota = quota_for([base, fallback])
    r = await gather_candidates("query", [base], quota, fallbacks=[fallback])
    assert "tavily" in r.providers_used
    assert fallback.calls == 1


async def test_no_escalation_when_candidates_are_plenty(quota_for):
    base = FakeProvider(
        "wikipedia", [cand(f"https://w{i}.example/{i}", snippet=f"keyword {i}") for i in range(5)]
    )
    fallback = FakeProvider("tavily", [cand("https://t.example/x")], cap=100)
    quota = quota_for([base, fallback])
    r = await gather_candidates("keyword", [base], quota, fallbacks=[fallback])
    assert "tavily" not in r.providers_used and fallback.calls == 0


async def test_advanced_depth_always_uses_fallback(quota_for):
    base = FakeProvider("wikipedia", [cand("https://w.example/1", snippet="x")])
    fallback = FakeProvider("tavily", [cand("https://t.example/x")], cap=100)
    quota = quota_for([base, fallback])
    r = await gather_candidates("q", [base], quota, fallbacks=[fallback], depth="advanced")
    assert "tavily" in r.providers_used


async def test_recency_words_escalate(quota_for):
    base = FakeProvider("wikipedia", [cand("https://w.example/1", snippet="x")])
    fallback = FakeProvider("tavily", [cand("https://t.example/x")], cap=100)
    quota = quota_for([base, fallback])
    r = await gather_candidates("latest postgres release news", [base], quota, fallbacks=[fallback])
    assert "tavily" in r.providers_used


async def test_fallback_moves_to_next_tier_on_failure(quota_for):
    base = FakeProvider("wikipedia", [cand("https://w.example/1")])
    brave = FakeProvider("brave", [cand("https://b.example/x")], cap=100)
    brave.error = RuntimeError("boom")
    tavily = FakeProvider("tavily", [cand("https://t.example/x")], cap=100)
    quota = quota_for([base, brave, tavily])
    r = await gather_candidates("q", [base], quota, fallbacks=[brave, tavily])
    assert r.providers_skipped["brave"] == "boom"
    assert "tavily" in r.providers_used and "brave" not in r.providers_used


async def test_fallback_skips_tier_without_quota(quota_for):
    base = FakeProvider("wikipedia", [cand("https://w.example/1")])
    brave = FakeProvider("brave", [cand("https://b.example/x")], cap=1)
    tavily = FakeProvider("tavily", [cand("https://t.example/x")], cap=100)
    quota = quota_for([base, brave, tavily])
    await quota.try_acquire("brave")  # exhaust brave
    r = await gather_candidates("q", [base], quota, fallbacks=[brave, tavily])
    assert r.providers_skipped["brave"] == "quota exhausted"
    assert "tavily" in r.providers_used


async def test_search_web_always_fires_even_when_not_thin(quota_for):
    base = FakeProvider(
        "wikipedia", [cand(f"https://w{i}.example/{i}", snippet=f"keyword {i}") for i in range(5)]
    )
    searxng = FakeProvider("searxng", [cand("https://s.example/x")])
    quota = quota_for([base, searxng])
    r = await gather_candidates("keyword", [base], quota, fallbacks=[searxng], search_web="always")
    assert "searxng" in r.providers_used and searxng.calls == 1


async def test_search_web_off_never_fires(quota_for):
    base = FakeProvider("wikipedia", [cand("https://w.example/1")])  # thin, would escalate
    searxng = FakeProvider("searxng", [cand("https://s.example/x")])
    quota = quota_for([base, searxng])
    r = await gather_candidates("q", [base], quota, fallbacks=[searxng], search_web="off")
    assert "searxng" not in r.providers_used and searxng.calls == 0


async def test_search_web_auto_fires_only_when_thin(quota_for):
    base = FakeProvider(
        "wikipedia", [cand(f"https://w{i}.example/{i}", snippet=f"keyword {i}") for i in range(5)]
    )
    searxng = FakeProvider("searxng", [cand("https://s.example/x")])
    quota = quota_for([base, searxng])
    r = await gather_candidates("keyword", [base], quota, fallbacks=[searxng], search_web="auto")
    assert "searxng" not in r.providers_used and searxng.calls == 0


async def test_include_domains_filters_fallback_results(quota_for):
    base = FakeProvider("wikipedia", [cand("https://en.wikipedia.org/wiki/A")])
    searxng = FakeProvider(
        "searxng", [cand("https://other.com/x"), cand("https://en.wikipedia.org/wiki/B")]
    )
    quota = quota_for([base, searxng])
    r = await gather_candidates(
        "q",
        [base],
        quota,
        fallbacks=[searxng],
        search_web="always",
        include_domains=["wikipedia.org"],
    )
    urls = [m.candidate.url for m in r.candidates]
    assert urls and all("wikipedia.org" in u for u in urls), urls
