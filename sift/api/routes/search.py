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
        response_time=round(elapsed, 3),
        providers_used=out.providers_used,
        providers_skipped=out.providers_skipped,
        cache_hit=out.cache_hit,
        timings_ms=out.timings_ms,
        tokens_used=out.tokens_used,
    )


@router.post("/v1/search", response_model=SearchResponse)
async def search(req: SearchRequest, request: Request, engine: EngineDep) -> SearchResponse:
    t0 = time.perf_counter()
    settings = engine.settings
    qcache: QueryCache = request.app.state.query_cache
    cache_key = query_cache_key(
        req.query,
        max_results=req.max_results,
        max_tokens=req.max_tokens,
        depth=req.search_depth,
        include_domains=req.include_domains,
        exclude_domains=req.exclude_domains,
        fresh_only=req.fresh_only,
        time_range=req.time_range,
        include_raw_content=req.include_raw_content,
        include_answer=req.include_answer,
    )
    ttl = (
        settings.query_cache_short_ttl
        if req.fresh_only or req.time_range in ("day", "d")
        else settings.query_cache_ttl
    )

    cached = await qcache.get(cache_key)
    if cached is not None:
        cached["response_time"] = round(time.perf_counter() - t0, 3)
        cached["cache_hit"] = True
        return SearchResponse(**cached)

    is_first = await qcache.acquire_lock(cache_key)
    if not is_first:
        waited = await qcache.wait_for(cache_key)
        if waited is not None:
            waited["response_time"] = round(time.perf_counter() - t0, 3)
            waited["cache_hit"] = True
            return SearchResponse(**waited)

    out = await run_search(
        engine,
        req.query,
        max_results=req.max_results,
        max_tokens=req.max_tokens,
        depth=req.search_depth,
        include_domains=req.include_domains,
        exclude_domains=req.exclude_domains,
        include_raw_content=req.include_raw_content,
        fresh_only=req.fresh_only,
        time_range=req.time_range,
        include_answer=req.include_answer,
    )
    if req.include_answer and out.answer is None and getattr(engine, "llm", None) is None:
        out.providers_skipped["answer"] = "no LLM configured"
    resp = _build_response(req, out, time.perf_counter() - t0)
    await qcache.set(cache_key, resp.model_dump(mode="json"), ttl)
    if is_first:
        await qcache.release_lock(cache_key)
    return resp
