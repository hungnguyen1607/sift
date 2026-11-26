import asyncio

from sift.pipeline.vertical_knn import VerticalKNN

VECS = {
    "web one": [1.0, 0.0, 0.0],
    "web two": [0.9, 0.1, 0.0],
    "jobs one": [0.0, 1.0, 0.0],
    "jobs two": [0.0, 0.95, 0.05],
    "anyone hiring devs": [0.05, 0.99, 0.0],  # close to the job examples
    "vague thing": [0.5, 0.5, 0.7],  # close to nothing
}


class Emb:
    async def embed_query(self, q):
        v = VECS[q]
        n = sum(x * x for x in v) ** 0.5
        return [x / n for x in v]


EXAMPLES = {"web": ["web one", "web two"], "jobs": ["jobs one", "jobs two"]}


async def ready_knn(**kw):
    knn = VerticalKNN(Emb(), EXAMPLES, k=2, **kw)
    knn.warm()
    await knn._task
    return knn


async def test_rules_decide_alone_until_examples_are_embedded():
    knn = VerticalKNN(Emb(), EXAMPLES, k=2)
    assert await knn.route("anyone hiring devs", "web") == "web"  # starts warming
    await knn._task
    assert knn.ready
    assert await knn.route("anyone hiring devs", "web") == "jobs"


async def test_only_close_confident_matches_override_web():
    knn = await ready_knn()
    assert await knn.route("vague thing", "web") == "web"


async def test_never_overrides_a_rule_match():
    knn = await ready_knn()
    assert await knn.route("anyone hiring devs", "people") == "people"


async def test_embedding_failure_keeps_rules():
    class Broken(Emb):
        async def embed_query(self, q):
            raise RuntimeError("model gone")

    knn = VerticalKNN(Broken(), EXAMPLES)
    knn.warm()
    await asyncio.sleep(0)
    await knn._task
    assert not knn.ready and await knn.route("x", "web") == "web"
