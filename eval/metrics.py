"""Metric functions for the Sift-vs-Tavily evaluation.

Labels are 0 (irrelevant), 1 (partial), 2 (answers). These are pure functions
over the judged results so the math is unit-testable without running a search.
"""

from __future__ import annotations

import math


def relevance_at_k(labels: list[int], k: int = 5) -> float:
    """Share of the top-k results labeled partial or better (>= 1)."""
    top = labels[:k]
    if not top:
        return 0.0
    return sum(1 for label in top if label >= 1) / len(top)


def dcg(labels: list[int], k: int) -> float:
    return sum((2**label - 1) / math.log2(i + 2) for i, label in enumerate(labels[:k]))


def ndcg_at_k(labels: list[int], k: int = 5) -> float:
    """nDCG@k with 0/1/2 gains (exponential gain: 2^l - 1)."""
    ideal = sorted(labels, reverse=True)[:k]
    idcg = dcg(ideal, k)
    return dcg(labels, k) / idcg if idcg else 0.0


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile (p in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * p / 100.0
    f, c = math.floor(k), math.ceil(k)
    if f == c:
        return s[f]
    return s[f] * (c - k) + s[c] * (k - f)


def token_efficiency(
    tokens_per_query: list[tuple[float, bool]],  # (tokens, answered)
) -> float | None:
    """Mean tokens per useful answer, where `answered` is a judge label of 2."""
    useful = [t for t, answered in tokens_per_query if answered]
    if not useful:
        return None
    return sum(useful) / len(useful)


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def group_metrics(
    judged: list[dict], systems: list[str] | None = None
) -> dict[str, dict[str, float | None]]:
    """judged: list of {query_id, system, labels}. Returns per-system aggregate
    metrics across all queries."""
    systems = systems or sorted({j["system"] for j in judged})
    by_system: dict[str, list[list[int]]] = {s: [] for s in systems}
    for j in judged:
        by_system.setdefault(j["system"], []).append(j["labels"])
    out: dict[str, dict[str, float | None]] = {}
    for system in systems:
        labels = by_system[system]
        out[system] = {
            "relevance@5": mean([relevance_at_k(x) for x in labels]),
            "ndcg@5": mean([ndcg_at_k(x) for x in labels]),
        }
    return out
