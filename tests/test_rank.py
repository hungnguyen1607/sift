import pytest

from sift.pipeline.candidates import RRF_K
from sift.pipeline.models import Page, Passage
from sift.pipeline.rank import bm25_scores, fuse, select, tokenize


def P(url, text, emb=None):
    return Passage(Page(url, "t", text, "x"), text, 0, len(text), embedding=emb)


def test_tokenize_folds_plurals_and_drops_stopwords():
    assert tokenize("How do I reverse the Lists in C++?") == ["reverse", "list", "c++"]


def test_bm25_prefers_matching_and_rarer_terms():
    docs = ["postgres jsonb gin index", "postgres replication", "cooking pasta at home"]
    s = bm25_scores("jsonb index postgres", docs)
    assert s[0] > s[1] > s[2] == 0


def test_fuse_is_rrf_over_lexical_and_vector():
    a = P("https://a.com", "a", emb=[1.0, 0.0])
    b = P("https://b.com", "b", emb=[0.0, 1.0])
    c = P("https://c.com", "c")
    ranked = fuse([a, b, c], lexical=[0.0, 2.0, 1.0], query_vec=[1.0, 0.0])
    score = {r.passage.page.url: r.score for r in ranked}
    assert score["https://b.com"] == pytest.approx(1 / (RRF_K + 1) + 1 / (RRF_K + 2))
    assert score["https://a.com"] == pytest.approx(1 / (RRF_K + 1))
    assert score["https://c.com"] == pytest.approx(1 / (RRF_K + 2))


def _ranked(passages):
    return fuse(passages, [float(len(passages) - i) for i in range(len(passages))], None)


def test_select_caps_results_per_site():
    ps = [P(f"https://a.com/{i}", f"unique text number {i} " * 5) for i in range(4)]
    ps.append(P("https://b.com/", "something else entirely here"))
    urls = [r.passage.page.url for r in select(_ranked(ps), 5, per_site=2)]
    assert urls == ["https://a.com/0", "https://a.com/1", "https://b.com/"]


def test_select_drops_near_duplicates():
    base = "the quick brown fox jumps over the lazy dog near the river bank today"
    ps = [
        P("https://a.com", base),
        P("https://b.com", base + " again"),
        P("https://c.com", "other words"),
    ]
    urls = [r.passage.page.url for r in select(_ranked(ps), 5)]
    assert urls == ["https://a.com", "https://c.com"]
