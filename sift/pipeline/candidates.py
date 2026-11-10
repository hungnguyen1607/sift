"""Candidate stage: query every provider in parallel within a time budget, then
merge their ranked link lists with Reciprocal Rank Fusion."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sift.pipeline.keywords import has_recency_words, keyword_query, looks_academic
from sift.providers.base import Candidate, Provider, QuotaManager

log = logging.getLogger(__name__)

RRF_K = 60
_TRACKING_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref_src")


def normalize_url(url: str) -> str:
    """Canonical form used to recognise the same page across providers."""
    parts = urlsplit(url.strip())
    host = parts.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith(_TRACKING_PREFIXES)
        )
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host.lower(), path, query, ""))


def domain_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def domain_matches(url: str, domains: list[str]) -> bool:
    host = domain_of(url)
    wanted = (d.lower().lstrip(".") for d in domains)
    return any(host == d or host.endswith("." + d) for d in wanted)


@dataclass(slots=True)
class MergedCandidate:
    candidate: Candidate
    score: float
    sources: list[str]


@dataclass(slots=True)
class CandidateResult:
    candidates: list[MergedCandidate]
    providers_used: list[str]
    providers_skipped: dict[str, str] = field(default_factory=dict)
    timings_ms: dict[str, int] = field(default_factory=dict)


def rrf_merge(ranked_lists: dict[str, list[Candidate]], k: int = RRF_K) -> list[MergedCandidate]:
    """score(url) = sum over providers of 1 / (k + rank), rank starting at 1.

    When providers return the same page, its scores add up, and the longest
    snippet and the earliest known dates are kept.
    """
    merged: dict[str, MergedCandidate] = {}
    for provider, items in ranked_lists.items():
        seen: set[str] = set()
        for rank, cand in enumerate(items, start=1):
            key = normalize_url(cand.url)
            if key in seen:  # a provider listing the same page twice counts once
                continue
            seen.add(key)
            contribution = 1.0 / (k + rank)
            m = merged.get(key)
            if m is None:
                merged[key] = MergedCandidate(cand, contribution, [provider])
                continue
            m.score += contribution
            m.sources.append(provider)
            best = m.candidate
            if len(cand.snippet) > len(best.snippet):
                best.snippet = cand.snippet
            best.published = best.published or cand.published
            best.updated = best.updated or cand.updated
    return sorted(merged.values(), key=lambda m: m.score, reverse=True)


def _thin(candidates: list[MergedCandidate], query: str) -> bool:
    """The "thin" escalation rule: fewer than 4 candidates, or none whose title
    or snippet contains a majority of the query keywords."""
    if len(candidates) < 4:
        return True
    keywords = [t for t in keyword_query(query).lower().split() if len(t) > 1]
    if not keywords:
        return False
    need = max(1, len(keywords) // 2 + 1)
    for m in candidates:
        hay = f"{m.candidate.title} {m.candidate.snippet}".lower()
        if sum(k in hay for k in keywords) >= need:
            return False
    return True


async def _run_fallback(
    fallbacks: list[Provider],
    query: str,
    keywords: str,
    per_provider: int,
    quota: QuotaManager,
    time_range: str | None = None,
) -> tuple[list[Candidate], str | None, dict[str, str]]:
    """Try fallback tiers in order, taking the first with quota left. A failure
    moves to the next tier."""
    skipped: dict[str, str] = {}
    for p in fallbacks:
        if p.rate_limit is not None and not await quota.try_rate(p.name, *p.rate_limit):
            skipped[p.name] = "rate limited"
            continue
        if not await quota.try_acquire(p.name):
            skipped[p.name] = "quota exhausted"
            continue
        try:
            items = await p.search(
                keywords if p.keyword_search else query, per_provider, time_range=time_range
            )
        except Exception as e:  # ProviderError or anything else
            skipped[p.name] = str(e) or type(e).__name__
            continue
        return items, p.name, skipped
    return [], None, skipped


async def gather_candidates(
    query: str,
    providers: list[Provider],
    quota: QuotaManager,
    *,
    per_provider: int = 10,
    budget_s: float = 1.5,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    fallbacks: list[Provider] | None = None,
    depth: str = "basic",
    time_range: str | None = None,
    search_web: str = "auto",
) -> CandidateResult:
    skipped: dict[str, str] = {}
    timings: dict[str, int] = {}
    keywords = keyword_query(query)
    fallbacks = [] if search_web == "off" else (fallbacks or [])

    async def run(p: Provider) -> list[Candidate]:
        t0 = time.perf_counter()
        try:
            return await p.search(
                keywords if p.keyword_search else query, per_provider, time_range=time_range
            )
        finally:
            timings[p.name] = round((time.perf_counter() - t0) * 1000)

    tasks: dict[asyncio.Task, Provider] = {}
    for p in providers:
        if p.academic_only and not looks_academic(query):
            skipped[p.name] = "not academic"
            continue
        if p.rate_limit is not None and not await quota.try_rate(p.name, *p.rate_limit):
            skipped[p.name] = "rate limited"
            continue
        if not await quota.try_acquire(p.name):
            skipped[p.name] = "quota exhausted"
            continue
        tasks[asyncio.create_task(run(p), name=f"provider:{p.name}")] = p

    # Recency and advanced queries escalate up front so the fallback runs in
    # parallel with the free providers instead of after them. `always` fires the
    # web backend on every cache miss (the route short-circuits cache hits).
    early = (search_web == "always" or depth == "advanced" or has_recency_words(query)) and bool(
        fallbacks
    )
    fb_task = None
    if early:
        fb_task = asyncio.create_task(
            _run_fallback(fallbacks, query, keywords, per_provider, quota, time_range),
            name="fallback",
        )

    done, pending = await asyncio.wait(tasks, timeout=budget_s) if tasks else (set(), set())
    for t in pending:
        t.cancel()
        skipped[tasks[t].name] = f"timeout after {budget_s}s"

    ranked: dict[str, list[Candidate]] = {}
    for t in done:
        name = tasks[t].name
        exc = t.exception()
        if exc is not None:
            log.info("provider %s failed: %s", name, exc)
            skipped[name] = str(exc) or type(exc).__name__
            continue
        items = t.result()
        if include_domains:
            items = [c for c in items if domain_matches(c.url, include_domains)]
        if exclude_domains:
            items = [c for c in items if not domain_matches(c.url, exclude_domains)]
        ranked[name] = items

    if early:
        fb_items, fb_name, fb_skipped = await fb_task
        skipped.update(fb_skipped)
        if fb_name and fb_items:
            ranked[fb_name] = fb_items
    elif fallbacks and _thin(list(rrf_merge(ranked)), query):
        fb_items, fb_name, fb_skipped = await _run_fallback(
            fallbacks, query, keywords, per_provider, quota, time_range
        )
        skipped.update(fb_skipped)
        if fb_name and fb_items:
            ranked[fb_name] = fb_items

    # Keep provider order stable for the response, regardless of finish order.
    order = [p.name for p in providers] + [p.name for p in fallbacks]
    ranked = {n: ranked[n] for n in order if n in ranked}
    return CandidateResult(
        candidates=rrf_merge(ranked),
        providers_used=[n for n, items in ranked.items() if items],
        providers_skipped=skipped,
        timings_ms=timings,
    )
