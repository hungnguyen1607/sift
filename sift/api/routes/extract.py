import time

from fastapi import APIRouter, Depends

from sift.api.deps import EngineDep
from sift.api.ratelimit import enforce_rate_limit
from sift.api.schemas import ExtractRequest, ExtractResponse
from sift.pipeline.extract import extract_urls

router = APIRouter(dependencies=[Depends(enforce_rate_limit)])


@router.post("/v1/extract", response_model=ExtractResponse)
async def extract(req: ExtractRequest, engine: EngineDep) -> ExtractResponse:
    t0 = time.perf_counter()
    results, failed = await extract_urls(engine, req.urls)
    return ExtractResponse(
        results=results,
        failed_results=failed,
        response_time=round(time.perf_counter() - t0, 3),
    )
