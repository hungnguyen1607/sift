from sift.pipeline.models import Page, Passage
from sift.pipeline.rank import Ranked
from sift.pipeline.verify import _evidence, _verdict, verify_claim


def rk(url, text):
    page = Page(url, "T", text, "x")
    return Ranked(Passage(page, text, 0, len(text)), 0.5)


def ev(url, conf):
    return {
        "url": url,
        "title": "T",
        "quote": "q",
        "quote_start": 0,
        "quote_end": 1,
        "confidence": conf,
    }


def test_verdict_supported_needs_two_sites():
    sup = [ev("https://a.com/x", 0.9), ev("https://b.com/y", 0.8)]
    assert _verdict(sup, []) == ("supported", 0.9)


def test_verdict_supported_rejected_if_one_site_only():
    sup = [ev("https://a.com/x", 0.9), ev("https://a.com/y", 0.8)]
    verdict, _ = _verdict(sup, [])
    assert verdict == "unclear"


def test_verdict_supported_blocked_by_contradiction():
    sup = [ev("https://a.com/x", 0.9), ev("https://b.com/y", 0.8)]
    contra = [ev("https://c.com/z", 0.75)]
    assert _verdict(sup, contra)[0] == "unclear"


def test_verdict_contradicted():
    contra = [ev("https://c.com/z", 0.9)]
    assert _verdict([], contra) == ("contradicted", 0.9)


def test_verdict_contradictions_must_outweigh_support():
    sup = [ev("https://a.com/x", 0.9), ev("https://b.com/y", 0.8)]
    contra = [ev("https://c.com/z", 0.85)]
    assert _verdict(sup, contra)[0] == "unclear"  # support 2 sites ≥0.7 outweighs 1 contradiction


def test_evidence_requires_exact_substring():
    passages = [rk("https://a.dev/", "the quick brown fox")]
    ok = {"id": 0, "label": "supporting", "confidence": 0.9, "quote": "quick brown"}
    kind, e = _evidence(ok, passages)
    assert kind == "supporting" and e["quote"] == "quick brown"
    assert e["quote_start"] == 4 and e["quote_end"] == 15

    bad = {"id": 0, "label": "supporting", "confidence": 0.9, "quote": "not in the text"}
    assert _evidence(bad, passages) is None

    unrelated = {"id": 0, "label": "unrelated", "confidence": 0.9, "quote": "quick"}
    assert _evidence(unrelated, passages) is None


def test_evidence_ignores_out_of_range_ids():
    passages = [rk("https://a.dev/", "text")]
    assert (
        _evidence({"id": 5, "label": "supporting", "confidence": 0.9, "quote": "text"}, passages)
        is None
    )


def _engine(cands, llm=None):
    import fakeredis

    from sift.api.deps import Engine
    from sift.config import Settings
    from sift.providers.base import QuotaManager

    from .conftest import FakeEmbedder, FakeIndex, FakeProvider, RecordingQueue

    providers = [FakeProvider("wikipedia", cands)]
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    quota = QuotaManager(r, {p.name: (None, "month") for p in providers})
    e = Engine(
        Settings(),
        providers,
        quota,
        embedder=FakeEmbedder(),
        index=FakeIndex(),
        enqueue=RecordingQueue(),
    )
    e.llm = llm
    return e


async def test_verify_claim_without_llm_is_unclear():
    from sift.providers.base import Candidate

    cands = [
        Candidate("https://a.dev/", "A", "", "wikipedia", content="Postgres supports jsonb " * 20)
    ]
    out = await verify_claim(_engine(cands), "postgres supports jsonb", max_sources=2)
    assert out["verdict"] == "unclear"
    assert out["supporting"] and out["contradicting"] == []


async def test_verify_claim_with_llm(monkeypatch):
    from sift.pipeline import search as search_mod

    passages = [
        rk("https://a.dev/", "Postgres supports jsonb"),
        rk("https://b.dev/", "MySQL also supports json"),
    ]

    class Outcome:
        results = passages

    async def fake_run_search(engine, claim, **kw):
        return Outcome()

    monkeypatch.setattr(search_mod, "run_search", fake_run_search)

    class FakeLLM:
        configured = True

        async def chat_json(self, system, user):
            return {
                "labels": [
                    {"id": 0, "label": "supporting", "confidence": 0.9, "quote": "supports jsonb"},
                    {"id": 1, "label": "supporting", "confidence": 0.8, "quote": "supports json"},
                ]
            }

    class E:
        llm = FakeLLM()

    out = await verify_claim(E(), "X supports json", max_sources=2)
    assert out["verdict"] == "supported"
    assert len(out["supporting"]) == 2 and out["contradicting"] == []
