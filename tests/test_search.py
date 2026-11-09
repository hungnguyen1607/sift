import pytest

from sift.api.deps import Engine
from sift.config import Settings
from sift.pipeline.chunk import chunk
from sift.pipeline.models import Page, Passage
from sift.pipeline.search import run_search
from sift.providers.base import Candidate

from .conftest import FakeEmbedder, FakeIndex, FakeProvider, RecordingQueue

SO_TEXT = (
    "# How do I reverse a list in Python?\n\nI have a list and want it backwards.\n\n"
    "## Accepted answer (score 1831)\n\nUse `reversed()` to iterate, or slicing to copy:\n\n"
    "```python\nfor x in reversed(xs):\n    print(x)\nys = xs[::-1]\n```"
)


def so_candidate():
    return Candidate(
        url="https://stackoverflow.com/questions/1/reverse",
        title="How do I reverse a list in Python?",
        snippet="I have a list",
        source="stackexchange",
        content=SO_TEXT,
    )


def engine(providers, index=None, queue=None):
    return Engine(
        settings=Settings(),
        providers=providers,
        quota=__import__("sift.providers.base", fromlist=["QuotaManager"]).QuotaManager(
            __import__("fakeredis").FakeAsyncRedis(decode_responses=True),
            {p.name: (p.cap, p.period) for p in providers},
        ),
        embedder=FakeEmbedder(),
        index=index or FakeIndex(),
        enqueue=queue,
    )


async def test_returns_exact_quotes_and_indexes_live_pages():
    q = RecordingQueue()
    e = engine([FakeProvider("stackexchange", [so_candidate()])], queue=q)
    out = await run_search(e, "how do I reverse a list in python", include_raw_content=True)
    assert out.results
    top = out.results[0].passage
    raw = out.page_texts["https://stackoverflow.com/questions/1/reverse"]
    assert raw[top.start : top.end] == top.text
    assert "reversed(xs)" in top.text
    assert not out.cache_hit
    await __import__("asyncio").sleep(0)  # let the background enqueue run
    assert [k for k, _ in q.jobs] == ["index_page"]
    assert q.jobs[0][1]["url"] == "https://stackoverflow.com/questions/1/reverse"


async def test_unfetchable_page_falls_back_to_snippet():
    class NoFetch:
        async def fetch(self, url):
            from sift.pipeline.fetch import FetchError

            raise FetchError("disallowed by robots.txt")

    c = Candidate(
        "https://blocked.example/x", "Reverse lists", "reverse a list with reversed()", "hn"
    )
    e = engine([FakeProvider("hn", [c])], queue=RecordingQueue())
    e.fetcher = NoFetch()
    out = await run_search(e, "reverse list")
    [r] = out.results
    assert r.passage.page.origin == "snippet"
    assert r.passage.text == "reverse a list with reversed()"


async def test_known_pages_come_from_the_index_without_fetching():
    page = Page(
        "https://stackoverflow.com/questions/1/reverse",
        "Reverse",
        SO_TEXT,
        "stackexchange",
        origin="index",
        sources=["local"],
    )
    passages = [
        Passage(page, SO_TEXT[s.start : s.end], s.start, s.end, s.heading) for s in chunk(SO_TEXT)
    ]
    q = RecordingQueue()
    idx = FakeIndex(known={"https://stackoverflow.com/questions/1/reverse": passages})
    e = engine([FakeProvider("stackexchange", [so_candidate()])], index=idx, queue=q)
    out = await run_search(e, "reverse a list python")
    assert out.cache_hit
    assert out.providers_used == ["local", "stackexchange"]
    assert out.results[0].passage.page.origin == "index"
    await __import__("asyncio").sleep(0)
    assert q.jobs == []


async def test_domain_filters_apply_to_local_hits():
    page = Page(
        "https://w3schools.com/x", "W3", "reverse list python tutorial", "web", origin="index"
    )
    hit = Passage(page, page.text, 0, len(page.text))
    e = engine([], index=FakeIndex(hits=[hit]))
    assert (await run_search(e, "reverse list python")).results
    assert not (
        await run_search(e, "reverse list python", exclude_domains=["w3schools.com"])
    ).results


@pytest.mark.parametrize("bad", ["embedder", "index"])
async def test_survives_component_failures(bad):
    class Boom:
        async def embed_query(self, q):
            raise RuntimeError("model gone")

        async def search(self, *a):
            raise RuntimeError("db gone")

        async def known_pages(self, urls):
            raise RuntimeError("db gone")

    e = engine([FakeProvider("stackexchange", [so_candidate()])])
    setattr(e, bad, Boom())
    out = await run_search(e, "reverse a list python")
    assert out.results


async def test_live_embedding_respects_budget_and_keeps_partial_results():
    import asyncio

    class SlowEmbedder(FakeEmbedder):
        # 15 ms per word: a 64-word live input takes ~1 s against a 0.8 s budget.
        def estimate_ms(self, texts):
            return 15.0 * sum(len(t.split()) for t in texts)

        async def embed_passages(self, texts):
            await asyncio.sleep(self.estimate_ms(texts) / 1000)
            return await super().embed_passages(texts)

    short = [
        Candidate(
            f"https://s{i}.dev/", f"Reverse {i}", "", "hn", content="reverse list python " * 5
        )
        for i in range(3)
    ]  # ~20-word inputs
    long_ = Candidate("https://long.dev/", "Long", "", "hn", content="reverse list python " * 100)
    e = engine([FakeProvider("hn", [*short, long_])])
    e.embedder = SlowEmbedder()
    t0 = asyncio.get_running_loop().time()
    out = await run_search(e, "reverse list python", max_results=10)
    assert asyncio.get_running_loop().time() - t0 < 1.5
    embedded = {r.passage.page.url for r in out.results if r.passage.embedding}
    assert embedded and "https://long.dev/" not in embedded
