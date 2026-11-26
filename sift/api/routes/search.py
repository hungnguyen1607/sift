import time

from fastapi import APIRouter, Depends, Request

from sift.api.deps import EngineDep
from sift.api.ratelimit import enforce_rate_limit
from sift.api.schemas import SearchRequest, SearchResponse, SearchResult
from sift.pipeline.cache import QueryCache, query_cache_key
from sift.pipeline.candidates import RRF_K, normalize_url
from sift.pipeline.search import run_search

router = APIRouter(dependencies=[Depends(enforce_rate_limit)])
# A passage ranked #1 by both the lexical and the vector ranking.
_BEST_SCORE = 2 / (RRF_K + 1)


def _build_response(req: SearchRequest, out, elapsed: float) -> SearchResponse:
    results = []
    for r in out.results:
        p, page = r.passage, r.passage.page
        exact = page.origin != "snippet"
        results.append(
            SearchResult(
                title=page.title,
                url=page.url,
                content=p.text,
                score=round(min(r.score / _BEST_SCORE, 1.0), 4),
                source=page.sources[0] if page.sources else page.source,
                sources=page.sources or [page.source],
                published=page.published,
                updated=page.updated,
                quote_start=p.start if exact else None,
                quote_end=p.end if exact else None,
                section=p.heading,
                fields=page.fields,
                raw_content=out.page_texts.get(normalize_url(page.url))
                if req.include_raw_content
                else None,
            )
        )
    return SearchResponse(
        query=req.query,
        answer=out.answer,
        results=results,
        sub_queries=out.sub_queries,
        vertical=out.vertical,
        detected_vertical=out.detected_vertical,
        fell_back_to_web=out.fell_back_to_web,
        response_time=round(elapsed, 3),
        providers_used=out.providers_used,
        providers_skipped=out.providers_skipped,
        cache_hit=out.cache_hit,
        timings_ms=out.timings_ms,
        tokens_used=out.tokens_used,
    )


def _cached(payload: dict, t0: float) -> SearchResponse:
    """A cache (or single-flight) hit, stamped with this request's own timing."""
    payload["response_time"] = round(time.perf_counter() - t0, 3)
    payload["cache_hit"] = True
    return SearchResponse(**payload)


async def _store(qcache: QueryCache, key: str, resp: SearchResponse, ttl: int, settings) -> None:
    if ttl != settings.query_cache_short_ttl:  # fresh-only requests stay short
        ttl = settings.vertical_cache_ttl.get(resp.vertical, ttl)
    payload = resp.model_dump(mode="json")
    await qcache.set(key, payload, ttl)
    await qcache.set_stale(key, payload, settings.stale_cache_ttl, time.time())


async def _stale_or(
    qcache: QueryCache, key: str, resp: SearchResponse, t0: float
) -> SearchResponse:
    """An empty answer is usually a transient upstream failure (engines
    rate-limiting): don't cache it, and fall back to the last good one."""
    prev = await qcache.get_stale(key)
    if prev is None:
        return resp
    payload, made_at = prev
    payload.update(
        stale=True,
        stale_age_s=max(0, int(time.time() - made_at)),
        response_time=round(time.perf_counter() - t0, 3),
        providers_skipped={**payload.get("providers_skipped", {}), **resp.providers_skipped},
    )
    return SearchResponse(**payload)


@router.post("/v1/search", response_model=SearchResponse)
async def search(req: SearchRequest, request: Request, engine: EngineDep) -> SearchResponse:
    t0 = time.perf_counter()
    settings = engine.settings
    qcache: QueryCache = request.app.state.query_cache
    # The same parameters key the cache and drive the search, so build them once.
    params = dict(
        max_results=req.max_results,
        max_tokens=req.max_tokens,
        depth=req.search_depth,
        include_domains=req.include_domains,
        exclude_domains=req.exclude_domains,
        fresh_only=req.fresh_only,
        time_range=req.time_range,
        include_raw_content=req.include_raw_content,
        include_answer=req.include_answer,
        vertical=req.vertical,
    )
    cache_key = query_cache_key(req.query, **params)
    ttl = (
        settings.query_cache_short_ttl
        if req.fresh_only or req.time_range in ("day", "d")
        else settings.query_cache_ttl
    )

    cached = await qcache.get(cache_key)
    if cached is not None:
        return _cached(cached, t0)

    is_first = await qcache.acquire_lock(cache_key)
    if not is_first:
        waited = await qcache.wait_for(cache_key)
        if waited is not None:
            return _cached(waited, t0)

    out = await run_search(engine, req.query, **params)
    if req.include_answer and out.answer is None and not getattr(engine.llm, "configured", False):
        out.providers_skipped["answer"] = "no LLM configured"
    resp = _build_response(req, out, time.perf_counter() - t0)
    if resp.results:
        await _store(qcache, cache_key, resp, ttl, settings)
    else:
        resp = await _stale_or(qcache, cache_key, resp, t0)
    if is_first:
        await qcache.release_lock(cache_key)
    return resp
