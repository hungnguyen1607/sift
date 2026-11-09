import asyncio
import logging
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from sift.api.deps import Engine
from sift.api.routes import search, usage
from sift.config import Settings, get_settings
from sift.index.db import create_pool, migrate
from sift.index.search import LocalIndex
from sift.pipeline.embed import FastEmbedder
from sift.pipeline.fetch import Fetcher
from sift.providers import (
    HackerNewsProvider,
    MarginaliaProvider,
    Provider,
    QuotaManager,
    StackExchangeProvider,
    WikipediaProvider,
)
from sift.worker import queue

log = logging.getLogger("sift")


def build_providers(settings: Settings, http: httpx.AsyncClient) -> list[Provider]:
    providers: list[Provider] = [
        WikipediaProvider(http, settings.wikipedia_lang),
        StackExchangeProvider(http, settings.stackexchange_key, settings.stackexchange_site),
        HackerNewsProvider(http),
        MarginaliaProvider(http, settings.marginalia_key),
    ]
    if settings.enabled_providers:
        wanted = {n.strip() for n in settings.enabled_providers.split(",")}
        providers = [p for p in providers if p.name in wanted]
    return providers


@asynccontextmanager
async def default_lifespan(app: FastAPI):
    settings = get_settings()
    pool = await create_pool(settings.database_url)
    applied = await migrate(pool)
    if applied:
        log.info("migrations applied: %s", applied)
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    http = httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(settings.provider_timeout + 0.5, connect=1.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    providers = build_providers(settings, http)
    embedder = FastEmbedder(threads=settings.embed_threads, cache_dir=settings.model_cache)
    await asyncio.to_thread(embedder.warmup)
    # trafilatura is CPU-bound; separate processes keep it off the event loop
    # and out of the GIL.
    cleaners = ProcessPoolExecutor(
        settings.clean_workers, mp_context=multiprocessing.get_context("forkserver")
    )

    async def enqueue(kind: str, payload: dict):
        return await queue.enqueue(pool, kind, payload)

    app.state.engine = Engine(
        settings=settings,
        providers=providers,
        quota=QuotaManager(redis, {p.name: (p.cap, p.period) for p in providers}),
        fetcher=Fetcher(
            http, redis, timeout=settings.fetch_timeout, max_bytes=settings.fetch_max_bytes
        ),
        embedder=embedder,
        index=LocalIndex(pool),
        enqueue=enqueue,
        clean_executor=cleaners,
    )
    log.info("ready: providers=%s", [p.name for p in providers])
    try:
        yield
    finally:
        cleaners.shutdown(wait=False, cancel_futures=True)
        embedder.close()
        await http.aclose()
        await redis.aclose()
        await pool.close()


def create_app(lifespan: Callable = default_lifespan) -> FastAPI:
    app = FastAPI(
        title="Sift",
        version="0.2.0",
        summary="Open-source, self-hostable search API for AI agents.",
        lifespan=lifespan,
    )
    app.include_router(search.router)
    app.include_router(usage.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
