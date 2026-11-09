import time

from fastapi import APIRouter

from sift.api.deps import EngineDep
from sift.api.schemas import SearchRequest, SearchResponse, SearchResult
from sift.pipeline.candidates import RRF_K, normalize_url
from sift.pipeline.search import run_search

router = APIRouter()
# A passage ranked #1 by both the lexical and the vector ranking.
_BEST_SCORE = 2 / (RRF_K + 1)


@router.post("/v1/search", response_model=SearchResponse)
async def search(req: SearchRequest, engine: EngineDep) -> SearchResponse:
    t0 = time.perf_counter()
    out = await run_search(
        engine,
        req.query,
        max_results=req.max_results,
        depth=req.search_depth,
        include_domains=req.include_domains,
        exclude_domains=req.exclude_domains,
        include_raw_content=req.include_raw_content,
    )
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
        results=results,
        response_time=round(time.perf_counter() - t0, 3),
        providers_used=out.providers_used,
        providers_skipped=out.providers_skipped,
        cache_hit=out.cache_hit,
        timings_ms=out.timings_ms,
    )
