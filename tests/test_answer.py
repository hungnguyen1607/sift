from sift.pipeline.answer import answer_prompt, generate_answer
from sift.pipeline.models import Page, Passage
from sift.pipeline.rank import Ranked


def rk(url, title, text):
    page = Page(url, title, text, "x")
    return Ranked(Passage(page, text, 0, len(text)), 0.5)


class FakeLLM:
    def __init__(self, answer=None, configured=True):
        self.answer = answer
        self.configured = configured
        self.prompt = None

    async def chat(self, system, user, timeout_s=None):
        self.prompt = (system, user)
        return self.answer


async def test_no_llm_returns_none():
    class E:
        llm = None

    assert await generate_answer(E(), "q", [rk("https://a.dev/", "T", "text")]) is None


async def test_unconfigured_llm_returns_none():
    assert (
        await generate_answer(
            type("E", (), {"llm": FakeLLM(configured=False)})(),
            "q",
            [rk("https://a.dev/", "T", "text")],
        )
        is None
    )


async def test_empty_results_return_none():
    class E:
        llm = FakeLLM("hi")

    assert await generate_answer(E(), "q", []) is None


async def test_answer_passes_numbered_sources():
    llm = FakeLLM("The answer cites [1].")
    engine = type("E", (), {"llm": llm})()
    ranked = [rk("https://a.dev/", "A", "alpha"), rk("https://b.dev/", "B", "beta")]
    assert await generate_answer(engine, "the question", ranked) == "The answer cites [1]."
    system, user = llm.prompt
    assert "[1]" in user and "https://a.dev/" in user and "beta" in user


def test_answer_prompt_numbered():
    ranked = [rk("https://a.dev/", "A", "alpha"), rk("https://b.dev/", "B", "beta")]
    prompt = answer_prompt("q?", ranked)
    assert prompt.index("[1]") < prompt.index("[2]")
    assert "alpha" in prompt and "beta" in prompt
