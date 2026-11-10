import pytest

from sift.pipeline.candidates import gather_candidates
from sift.pipeline.keywords import keyword_query

from .conftest import FakeProvider


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("compare Postgres and MySQL for JSON workloads", "Postgres MySQL JSON workloads"),
        ("how do I reverse a list in python?", "reverse list python"),
        ("what's new in node.js 22 vs c++ 23", "new node.js 22 c++ 23"),
        ('docs for "connection pool" in asyncpg', 'docs "connection pool" asyncpg'),
        ("what is it", "what is it"),  # nothing left: fall back to the original
    ],
)
def test_keyword_query(question, expected):
    assert keyword_query(question) == expected


async def test_only_keyword_providers_get_rewritten_query(quota_for):
    seen = {}

    class Recorder(FakeProvider):
        async def search(self, query, limit, time_range=None):
            seen[self.name] = query
            return []

    upstream = Recorder("wikipedia")
    local = Recorder("local")
    local.keyword_search = False
    await gather_candidates("how do I use jsonb", [upstream, local], quota_for([upstream, local]))
    assert seen == {"wikipedia": "use jsonb", "local": "how do I use jsonb"}
