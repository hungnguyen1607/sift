"""Candidate stage: query every provider in parallel within a time budget, then
merge their ranked link lists with Reciprocal Rank Fusion."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sift.pipeline.keywords import keyword_query
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


async def gather_candidates(
    query: str,
    providers: list[Provider],
    quota: QuotaManager,
    *,
    per_provider: int = 10,
    budget_s: float = 1.5,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
) -> CandidateResult:
    skipped: dict[str, str] = {}
    timings: dict[str, int] = {}
    keywords = keyword_query(query)

    async def run(p: Provider) -> list[Candidate]:
        t0 = time.perf_counter()
        try:
            return await p.search(keywords if p.keyword_search else query, per_provider)
        finally:
            timings[p.name] = round((time.perf_counter() - t0) * 1000)

    tasks: dict[asyncio.Task, Provider] = {}
    for p in providers:
        if not await quota.try_acquire(p.name):
            skipped[p.name] = "quota exhausted"
            continue
        tasks[asyncio.create_task(run(p), name=f"provider:{p.name}")] = p

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

    # Keep provider order stable for the response, regardless of finish order.
    order = [p.name for p in providers]
    ranked = {n: ranked[n] for n in order if n in ranked}
    return CandidateResult(
        candidates=rrf_merge(ranked),
        providers_used=[n for n, items in ranked.items() if items],
        providers_skipped=skipped,
        timings_ms=timings,
    )
