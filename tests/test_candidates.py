import pytest

from sift.pipeline.candidates import (
    RRF_K,
    domain_matches,
    gather_candidates,
    normalize_url,
    rrf_merge,
)
from sift.providers.base import ProviderError

from .conftest import FakeProvider, cand


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("https://www.Example.com/a/", "https://example.com/a"),
        ("http://example.com/a#section", "https://example.com/a"),
        ("https://example.com/a?utm_source=x&b=2&a=1", "https://example.com/a?a=1&b=2"),
        ("https://example.com", "https://example.com/"),
    ],
)
def test_normalize_url_equivalences(a, b):
    assert normalize_url(a) == normalize_url(b)


def test_normalize_url_keeps_meaningful_differences():
    assert normalize_url("https://example.com/a?id=1") != normalize_url(
        "https://example.com/a?id=2"
    )
    assert normalize_url("https://example.com:8080/") != normalize_url("https://example.com/")


def test_rrf_math():
    merged = rrf_merge(
        {
            "p1": [cand("https://a.com"), cand("https://b.com")],
            "p2": [cand("https://b.com"), cand("https://c.com")],
        }
    )
    scores = {m.candidate.url: m.score for m in merged}
    assert scores["https://b.com"] == pytest.approx(1 / (RRF_K + 2) + 1 / (RRF_K + 1))
    assert scores["https://a.com"] == pytest.approx(1 / (RRF_K + 1))
    assert scores["https://c.com"] == pytest.approx(1 / (RRF_K + 2))
    assert [m.candidate.url for m in merged] == ["https://b.com", "https://a.com", "https://c.com"]
    assert merged[0].sources == ["p1", "p2"]


def test_rrf_dedups_and_keeps_richest_snippet():
    merged = rrf_merge(
        {
            "p1": [cand("https://www.a.com/x/", snippet="short"), cand("https://a.com/x")],
            "p2": [cand("https://a.com/x", snippet="a much longer snippet")],
        }
    )
    assert len(merged) == 1
    assert merged[0].candidate.snippet == "a much longer snippet"
    assert merged[0].score == pytest.approx(2 / (RRF_K + 1))


def test_domain_matching_includes_subdomains_only():
    assert domain_matches("https://docs.python.org/3/", ["python.org"])
    assert domain_matches("https://www.python.org/", ["python.org"])
    assert not domain_matches("https://notpython.org/", ["python.org"])


async def test_slow_and_failing_providers_do_not_block(quota_for):
    fast = FakeProvider("fast", [cand("https://a.com")])
    slow = FakeProvider("slow", [cand("https://b.com")], delay=5)
    broken = FakeProvider("broken", error=ProviderError("broken: HTTP 500"))
    providers = [fast, slow, broken]
    res = await gather_candidates("q", providers, quota_for(providers), budget_s=0.2)
    assert res.providers_used == ["fast"]
    assert res.providers_skipped == {"slow": "timeout after 0.2s", "broken": "broken: HTTP 500"}
    assert [m.candidate.url for m in res.candidates] == ["https://a.com"]


async def test_domain_filters(quota_for):
    p = FakeProvider(
        "p",
        [cand("https://docs.python.org/a"), cand("https://w3schools.com/b"), cand("https://c.io")],
    )
    q = quota_for([p])
    inc = await gather_candidates("q", [p], q, include_domains=["python.org", "c.io"])
    assert [m.candidate.url for m in inc.candidates] == [
        "https://docs.python.org/a",
        "https://c.io",
    ]
    exc = await gather_candidates("q", [p], q, exclude_domains=["w3schools.com"])
    assert "https://w3schools.com/b" not in [m.candidate.url for m in exc.candidates]
