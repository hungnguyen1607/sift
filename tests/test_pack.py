import random

from sift.pipeline.models import Page, Passage
from sift.pipeline.pack import count_tokens, pack, result_tokens, truncate_tokens
from sift.pipeline.rank import Ranked


def r(page_url, text):
    page = Page(page_url, "Title " + page_url, text, "x")
    return Ranked(Passage(page, text, 0, len(text)), 1.0)


def total(rks):
    return sum(result_tokens(x.passage.page.title, x.passage.page.url, x.passage.text) for x in rks)


def test_pack_never_exceeds_budget():
    rng = random.Random(7)
    for _ in range(200):
        n = rng.randint(0, 30)
        items = [r(f"https://s{i}.dev/", "word " * rng.randint(1, 600)) for i in range(n)]
        budget = rng.randint(1, 4000)
        kept, used = pack(items, budget)
        assert used is not None
        assert total(kept) <= budget
        assert used == total(kept)


def test_pack_keeps_best_first_and_never_splits():
    small = r("https://a.dev/", "tiny passage")
    big = r("https://b.dev/", "large passage " * 100)
    medium = r("https://c.dev/", "medium passage " * 20)
    budget = result_tokens(big.passage.page.title, big.passage.page.url, big.passage.text)
    kept, _ = pack([small, big, medium], budget)
    # the big one is skipped, but small + medium still fit after it
    assert kept[0] is small and big not in kept and medium in kept


def test_budget_smaller_than_any_passage_returns_empty():
    huge = r("https://a.dev/", "long passage " * 500)
    kept, used = pack([huge], 10)
    assert kept == [] and used == 0


def test_no_budget_returns_everything():
    items = [r("https://a.dev/", "x"), r("https://b.dev/", "y")]
    kept, used = pack(items, None)
    assert kept == items and used is None


def test_token_count_is_reasonable():
    assert count_tokens("hello world") == 2
    assert count_tokens("") == 0
    # truncate keeps roughly within the token limit
    long = " ".join(["token"] * 500)
    cut = truncate_tokens(long, 50)
    assert count_tokens(cut) <= 50
    assert cut.startswith("token")
