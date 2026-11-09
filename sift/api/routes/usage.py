from fastapi import APIRouter

from sift.api.deps import EngineDep

router = APIRouter()


@router.get("/v1/usage")
async def usage(engine: EngineDep) -> dict:
    """Free-tier budget per provider for the current day/month."""
    return {"providers": await engine.quota.usage()}
