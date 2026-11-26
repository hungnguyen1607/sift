from fastapi import APIRouter

from sift.api.deps import EngineDep
from sift.providers.searxng import SearXNGProvider

router = APIRouter()


@router.get("/v1/usage")
async def usage(engine: EngineDep) -> dict:
    """Free-tier budget per provider for the current day/month."""
    return {"providers": await engine.quota.usage()}


@router.get("/v1/health")
async def health(engine: EngineDep) -> dict:
    """Which search engines are usable right now. Engines that rate-limited
    Sift or showed a CAPTCHA are skipped until ``retry_in_s``; when most are
    cooling, list searches come back thin (or stale) until they recover."""
    sx = next((p for p in engine.fallbacks if isinstance(p, SearXNGProvider)), None)
    engines = sx.health.snapshot() if sx else {}
    cooling = [n for n, e in engines.items() if e["cooling"]]
    return {
        "searxng": sx is not None,
        "engines_cooling": cooling,
        "engines": engines,
        "providers": [p.name for p in engine.providers],
        "fallbacks": [p.name for p in engine.fallbacks],
        "vertical_providers": sorted(engine.vertical_providers),
    }
