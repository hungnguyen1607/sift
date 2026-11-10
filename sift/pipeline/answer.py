"""Optional answer model (`include_answer=true`).

Off by default. When no LLM is configured the answer is `null` (and the route
records it in ``providers_skipped``). With one, the packed passages are passed
numbered and the model must cite `[n]` and refuse when the passages don't
contain the answer. The answer is never cached on its own — it lives in the
query cache.
"""

from __future__ import annotations

import logging

from sift.pipeline.rank import Ranked

log = logging.getLogger(__name__)

SYSTEM = (
    "Answer the question using only the numbered sources below. Cite sources "
    "inline as [n]. If the sources do not contain the answer, say you cannot "
    "answer from the given sources. Be concise."
)


def answer_prompt(query: str, ranked: list[Ranked]) -> str:
    blocks = [
        f"[{i}] {r.passage.page.title}\n{r.passage.page.url}\n{r.passage.text}"
        for i, r in enumerate(ranked, 1)
    ]
    return f"Question: {query}\n\nSources:\n\n" + "\n\n".join(blocks)


async def generate_answer(engine, query: str, ranked: list[Ranked]) -> str | None:
    llm = getattr(engine, "llm", None)
    if llm is None or not llm.configured or not ranked:
        return None
    answer = await llm.chat(SYSTEM, answer_prompt(query, ranked))
    return (answer or "").strip() or None
