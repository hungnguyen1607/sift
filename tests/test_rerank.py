from sift.api.deps import Engine
from sift.config import Settings
from sift.pipeline.keywords import has_recency_words, looks_academic
from sift.pipeline.models import Page, Passage
from sift.pipeline.rank import Ranked
from sift.pipeline.search import _rerank_top


def ranked(items):
    out = []
    for i, text in enumerate(items):
        page = Page(f"https://s{i}.dev/", f"T{i}", text, "x")
        out.append(Ranked(Passage(page, text, 0, len(text)), 0.5 - i * 0.01))
    return out


class FakeReranker:
    def __init__(self, scores):
        self.scores = scores

    async def rerank(self, query, docs, budget_s):
        return self.scores


def test_looks_academic():
    assert looks_academic("a paper about transformers")
    assert looks_academic("et al citation")
    assert looks_academic("theorem proof")
    assert not looks_academic("how to reverse a list in python")


def test_has_recency_words():
    assert has_recency_words("latest postgres release")
    assert has_recency_words("news today")
    assert not has_recency_words("postgres jsonb tutorial")


async def test_rerank_top_reorders_by_score():
    items = ranked(["aaa", "bbb", "ccc", "ddd", "eee", "fff"])
    engine = Engine(
        Settings(),
        [],
        __import__("sift.providers.base", fromlist=["QuotaManager"]).QuotaManager(
            __import__("fakeredis").FakeAsyncRedis(decode_responses=True), {}
        ),
    )
    engine.reranker = FakeReranker([0.9, 0.1, 0.5, None, None, None])
    await _rerank_top(engine, "q", items)
    # completed (index 0,2,1) sorted by score desc, uncompleted (3,4) keep order
    assert [r.score for r in items[:3]] == [0.9, 0.5, 0.1]
    assert [r.passage.text for r in items[:5]] == ["aaa", "ccc", "bbb", "ddd", "eee"]
