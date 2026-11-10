"""Query planning: split a multi-part question into sub-queries.

Rule-based by default (no LLM needed). The rules cover comparisons ("X vs Y"),
several questions in one ("What is X? How do I Y?"), and lists ("pros and cons
of X, Y and Z"). At most 3 sub-queries; a query that matches nothing passes
through unchanged. When ``SIFT_LLM_*`` is configured, an LLM planner is tried
first and the rules are the fallback.
"""

from __future__ import annotations

import logging
import re

MAX_SUBQUERIES = 3

log = logging.getLogger(__name__)

# "compare X and Y (for Z)", "X vs Y (for Z)", "difference between X and Y",
# "X or Y for Z". The trailing context Z is appended to both sides.
_COMPARE_RES = [
    re.compile(
        r"^(?:compare|comparing)\s+(.+?)\s+(?:and|&)\s+(.+?)(?:\s+(?:for|in terms of|"
        r"with regard to|regarding|on|when it comes to)\s+(.+))?$",
        re.I,
    ),
    re.compile(
        r"^(.+?)\s+(?:vs\.?|versus)\s+(.+?)(?:\s+(?:for|in terms of|with regard to|"
        r"regarding|on|when it comes to)\s+(.+))?$",
        re.I,
    ),
    re.compile(
        r"^(?:what(?:'s|s| is| are)?\s+)?(?:the\s+)?(?:difference|differences)\s+between\s+"
        r"(.+?)\s+(?:and|&)\s+(.+?)(?:\s+(?:for|in terms of|with regard to|regarding|on|"
        r"when it comes to)\s+(.+))?$",
        re.I,
    ),
    re.compile(r"^(.+?)\s+or\s+(.+?)\s+(?:for|in)\s+(.+)$", re.I),
]

# "pros and cons of X, Y and Z" → one sub-query per item with the shared context.
_LIST_RE = re.compile(r"^(pros and cons of|advantages and disadvantages of)\s+(.+)$", re.I)
_SPLIT_RE = re.compile(r"\s*,\s*|\s+(?:and|or)\s+", re.I)


def _subs(parts: list[str], context: str = "") -> list[str]:
    out = []
    for p in parts:
        p = p.strip().strip('"').strip("?")
        if not p:
            continue
        out.append(f"{p} {context}".strip() if context else p)
    return out[:MAX_SUBQUERIES]


def _compare_split(query: str) -> list[str]:
    for rx in _COMPARE_RES:
        m = rx.match(query)
        if not m:
            continue
        groups = m.groups()
        a, b = groups[0], groups[1]
        ctx = groups[2] if len(groups) > 2 and groups[2] else ""
        subs = _subs([a, b], ctx)
        if len(subs) >= 2:
            return subs
    return []


def _question_split(query: str) -> list[str]:
    """Several sentences ending in '?' become one sub-query each."""
    parts = [p.strip() for p in query.split("?") if p.strip()]
    if len(parts) >= 2:
        return parts[:MAX_SUBQUERIES]
    return []


def _list_split(query: str) -> list[str]:
    m = _LIST_RE.match(query)
    if not m:
        return []
    context, body = m.group(1), m.group(2)
    items = [i.strip() for i in _SPLIT_RE.split(body) if i.strip()]
    if len(items) < 2:
        return []
    return [f"{context} {i}" for i in items][:MAX_SUBQUERIES]


def plan(query: str) -> list[str]:
    """Split a query into 1–3 sub-queries, or return the query unchanged."""
    q = query.strip()
    if not q:
        return []
    for splitter in (_compare_split, _question_split, _list_split):
        subs = splitter(q)
        if len(subs) >= 2:
            return subs
    return [q]


async def plan_queries(engine, query: str) -> list[str]:
    """Plan with the LLM when configured, falling back to the rules."""
    llm = getattr(engine, "llm", None)
    if llm is None or not llm.configured:
        return plan(query)
    system = (
        "You split a single user question into independent sub-queries for a search engine. "
        'Reply with JSON only: {"sub_queries": ["...", ...]} — at most 3, in the same '
        "language as the question. If the question is single-part, return one element."
    )
    try:
        result = await llm.chat_json(system, query)
    except Exception as e:  # pragma: no cover - defensive
        log.info("LLM planner failed: %r", e)
        result = None
    subs = (result or {}).get("sub_queries") if isinstance(result, dict) else None
    if isinstance(subs, list) and subs and all(isinstance(s, str) for s in subs):
        return [s.strip() for s in subs if s.strip()][:MAX_SUBQUERIES] or [query]
    return plan(query)
