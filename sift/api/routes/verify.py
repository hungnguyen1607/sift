import time

from fastapi import APIRouter, Depends

from sift.api.deps import EngineDep
from sift.api.ratelimit import enforce_rate_limit
from sift.api.schemas import VerifyRequest, VerifyResponse
from sift.pipeline.verify import verify_claim

router = APIRouter(dependencies=[Depends(enforce_rate_limit)])


@router.post("/v1/verify", response_model=VerifyResponse)
async def verify(req: VerifyRequest, engine: EngineDep) -> VerifyResponse:
    t0 = time.perf_counter()
    out = await verify_claim(engine, req.claim, req.max_sources)
    return VerifyResponse(
        verdict=out["verdict"],
        confidence=out["confidence"],
        supporting=out["supporting"],
        contradicting=out["contradicting"],
        response_time=round(time.perf_counter() - t0, 3),
    )
