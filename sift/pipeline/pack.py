"""Token-budget packing: keep the best passages that fit in ``max_tokens``.

Counts tokens with tiktoken's ``o200k_base`` (falling back to a chars/4
estimate if tiktoken is unavailable), adds per-result overhead for the title,
URL and JSON keys, and greedily fills the budget without ever cutting a
passage.
"""

from __future__ import annotations

from functools import lru_cache

from sift.pipeline.rank import Ranked

# JSON keys plus title/URL boilerplate per returned result.
RESULT_OVERHEAD = 20


@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken

        return tiktoken.get_encoding("o200k_base")
    except Exception:  # missing package or unknown encoding → estimate
        return None


@lru_cache(maxsize=200_000)
def count_tokens(text: str) -> int:
    enc = _encoding()
    if enc is None:
        return max(1, len(text) // 4)
    return len(enc.encode(text, disallowed_special=()))


def result_tokens(title: str, url: str, content: str) -> int:
    return count_tokens(content) + count_tokens(title) + count_tokens(url) + RESULT_OVERHEAD


def truncate_tokens(text: str, max_tokens: int) -> str:
    """Cut ``text`` to roughly ``max_tokens`` without splitting a word."""
    enc = _encoding()
    if enc is None:
        cut = text[: max_tokens * 4]
    else:
        cut = enc.decode(enc.encode(text, disallowed_special=())[:max_tokens])
    return cut.rsplit(" ", 1)[0] if " " in cut and len(cut) > max_tokens * 2 else cut


def pack(ranked: list[Ranked], max_tokens: int | None) -> tuple[list[Ranked], int | None]:
    """Greedily keep passages in rank order while the total fits the budget.
    A passage that doesn't fit is skipped (later, smaller ones still can).
    ``tokens_used`` is None when there is no budget."""
    if max_tokens is None:
        return ranked, None
    kept: list[Ranked] = []
    used = 0
    for r in ranked:
        cost = result_tokens(r.passage.page.title, r.passage.page.url, r.passage.text)
        if used + cost > max_tokens:
            continue
        kept.append(r)
        used += cost
    return kept, used
