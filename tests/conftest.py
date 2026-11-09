import asyncio

import fakeredis
import pytest

from sift.providers.base import Candidate, Provider, QuotaManager


class FakeProvider(Provider):
    def __init__(self, name, results=(), *, delay=0.0, error=None, cap=None, period="month"):
        self.name = name
        self.results = list(results)
        self.delay = delay
        self.error = error
        self.cap = cap
        self.period = period
        self.calls = 0

    async def search(self, query, limit):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.results[:limit]


def cand(url, source="x", title=None, snippet=""):
    return Candidate(url=url, title=title or url, snippet=snippet, source=source)


@pytest.fixture
def redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def quota_for(redis):
    def make(providers):
        return QuotaManager(redis, {p.name: (p.cap, p.period) for p in providers})

    return make


class FakeEmbedder:
    """Deterministic hashed bag-of-words vectors: similar words → similar vectors."""

    def __init__(self):
        self.passage_calls = 0

    def _vec(self, text):
        import hashlib

        from sift.pipeline.embed import DIM, normalize
        from sift.pipeline.rank import tokenize

        v = [0.0] * DIM
        for tok in tokenize(text):
            v[int(hashlib.md5(tok.encode()).hexdigest(), 16) % DIM] += 1.0
        return normalize(v)

    async def embed_query(self, query):
        return self._vec(query)

    async def embed_passages(self, texts):
        self.passage_calls += 1
        return [self._vec(t) for t in texts]

    def estimate_ms(self, texts):
        return 0.0


class FakeIndex:
    def __init__(self, hits=(), known=None, texts=None):
        self.hits = list(hits)
        self.known = known or {}
        self.texts = texts or {}

    async def search(self, query, qvec, limit):
        return self.hits[:limit]

    async def known_pages(self, urls):
        from sift.pipeline.candidates import normalize_url

        return {
            normalize_url(u): self.known[normalize_url(u)]
            for u in urls
            if normalize_url(u) in self.known
        }

    async def page_texts(self, urls):
        from sift.pipeline.candidates import normalize_url

        return {
            normalize_url(u): self.texts[normalize_url(u)]
            for u in urls
            if normalize_url(u) in self.texts
        }


class RecordingQueue:
    def __init__(self):
        self.jobs = []

    async def __call__(self, kind, payload):
        self.jobs.append((kind, payload))
