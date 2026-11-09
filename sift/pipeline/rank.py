"""Passage ranking: BM25 + vector similarity fused with RRF, then
near-duplicate removal and a per-site cap for source diversity."""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

from sift.pipeline.candidates import RRF_K, domain_of, normalize_url
from sift.pipeline.embed import cosine
from sift.pipeline.keywords import _STOPWORDS
from sift.pipeline.models import Passage

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#]*")


def tokenize(text: str) -> list[str]:
    out = []
    for t in _TOKEN_RE.findall(text.lower()):
        if t in _STOPWORDS:
            continue
        if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]  # crude plural folding: "indexes" ~ "indexe", "lists" ~ "list"
        out.append(t)
    return out


def bm25_scores(query: str, docs: list[str], k1: float = 1.2, b: float = 0.75) -> list[float]:
    """BM25 with the candidate pool itself as the corpus."""
    q_terms = set(tokenize(query))
    if not q_terms or not docs:
        return [0.0] * len(docs)
    toks = [tokenize(d) for d in docs]
    n = len(docs)
    avgdl = sum(len(t) for t in toks) / n or 1.0
    df = Counter(term for t in toks for term in set(t) if term in q_terms)
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q_terms}
    scores = []
    for t in toks:
        tf = Counter(x for x in t if x in q_terms)
        dl = len(t)
        scores.append(
            sum(idf[w] * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / avgdl)) for w, f in tf.items())
        )
    return scores


@dataclass(slots=True)
class Ranked:
    passage: Passage
    score: float
    lexical_rank: int | None = None
    vector_rank: int | None = None


def fuse(
    passages: list[Passage],
    lexical: list[float],
    query_vec: list[float] | None,
    k: int = RRF_K,
) -> list[Ranked]:
    """RRF over the lexical order and the vector order. Passages with no
    lexical match don't get a lexical rank; passages with no embedding don't
    get a vector rank."""
    ranked = [Ranked(p, 0.0) for p in passages]
    lex_order = sorted((i for i, s in enumerate(lexical) if s > 0), key=lambda i: -lexical[i])
    for r, i in enumerate(lex_order, start=1):
        ranked[i].lexical_rank = r
        ranked[i].score += 1 / (k + r)
    if query_vec is not None:
        sims = [(i, cosine(query_vec, p.embedding)) for i, p in enumerate(passages) if p.embedding]
        sims.sort(key=lambda x: -x[1])
        for r, (i, _) in enumerate(sims, start=1):
            ranked[i].vector_rank = r
            ranked[i].score += 1 / (k + r)
    ranked.sort(key=lambda x: -x.score)
    return ranked


def _shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = text.lower().split()
    if len(words) < n:
        return {tuple(words)}
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def select(
    ranked: list[Ranked], limit: int, per_site: int = 2, dup_threshold: float = 0.7
) -> list[Ranked]:
    """Walk the ranking best-first, skipping exact repeats, near-duplicates
    (word 5-gram Jaccard ≥ threshold) and sites that already have
    ``per_site`` results."""
    out: list[Ranked] = []
    seen_spans: set[tuple[str, int, int]] = set()
    kept_shingles: list[set] = []
    per_site_count: Counter[str] = Counter()
    for r in ranked:
        if r.score <= 0:
            break
        p = r.passage
        span = (normalize_url(p.page.url), p.start, p.end)
        if span in seen_spans:
            continue
        site = domain_of(p.page.url)
        if per_site_count[site] >= per_site:
            continue
        sh = _shingles(p.text)
        if any(len(sh & o) / (len(sh | o) or 1) >= dup_threshold for o in kept_shingles):
            continue
        seen_spans.add(span)
        kept_shingles.append(sh)
        per_site_count[site] += 1
        out.append(r)
        if len(out) >= limit:
            break
    return out
