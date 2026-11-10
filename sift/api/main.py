import asyncio
import logging
import multiprocessing
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from redis.asyncio import Redis

from sift.api.auth import KeyStore
from sift.api.deps import Engine
from sift.api.ratelimit import RateLimiter
from sift.api.routes import extract, search, usage, verify
from sift.config import Settings, get_settings
from sift.index.db import create_pool, migrate
from sift.index.search import LocalIndex
from sift.pipeline.cache import PageCache, QueryCache
from sift.pipeline.embed import FastEmbedder
from sift.pipeline.fetch import Fetcher
from sift.pipeline.llm import LLM
from sift.providers import (
    ArxivProvider,
    BraveProvider,
    GitHubProvider,
    HackerNewsProvider,
    MarginaliaProvider,
    OpenAlexProvider,
    Provider,
    QuotaManager,
    SearXNGProvider,
    SerperProvider,
    StackExchangeProvider,
    TavilyProvider,
    WikipediaProvider,
)
from sift.worker import queue

log = logging.getLogger("sift")


def build_providers(
    settings: Settings, http: httpx.AsyncClient
) -> tuple[list[Provider], list[Provider]]:
    providers: list[Provider] = [
        WikipediaProvider(http, settings.wikipedia_lang),
        StackExchangeProvider(http, settings.stackexchange_key, settings.stackexchange_site),
        HackerNewsProvider(http),
        MarginaliaProvider(http, settings.marginalia_key),
        OpenAlexProvider(http, settings.openalex_mailto),
        ArxivProvider(http),
    ]
    if settings.github_token:
        providers.append(GitHubProvider(http, settings.github_token))

    fallbacks: list[Provider] = []
    if settings.searxng_url:
        fallbacks.append(SearXNGProvider(http, settings.searxng_url))
    if settings.brave_key:
        fallbacks.append(BraveProvider(http, settings.brave_key))
    if settings.tavily_key:
        fallbacks.append(TavilyProvider(http, settings.tavily_key))
    if settings.serper_key:
        fallbacks.append(SerperProvider(http, settings.serper_key))

    if settings.enabled_providers:
        wanted = {n.strip() for n in settings.enabled_providers.split(",")}
        providers = [p for p in providers if p.name in wanted]
        fallbacks = [p for p in fallbacks if p.name in wanted]
    return providers, fallbacks


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
    providers, fallbacks = build_providers(settings, http)
    embedder = FastEmbedder(threads=settings.embed_threads, cache_dir=settings.model_cache)
    await asyncio.to_thread(embedder.warmup)
    # trafilatura is CPU-bound; separate processes keep it off the event loop
    # and out of the GIL.
    cleaners = ProcessPoolExecutor(
        settings.clean_workers, mp_context=multiprocessing.get_context("forkserver")
    )

    async def enqueue(kind: str, payload: dict):
        return await queue.enqueue(pool, kind, payload)

    all_providers = [*providers, *fallbacks]
    app.state.engine = Engine(
        settings=settings,
        providers=providers,
        fallbacks=fallbacks,
        quota=QuotaManager(redis, {p.name: (p.cap, p.period) for p in all_providers}),
        fetcher=Fetcher(
            http, redis, timeout=settings.fetch_timeout, max_bytes=settings.fetch_max_bytes
        ),
        embedder=embedder,
        index=LocalIndex(pool),
        enqueue=enqueue,
        clean_executor=cleaners,
        page_cache=PageCache(redis, ttl=settings.url_cache_ttl),
        llm=LLM(
            settings.llm_provider,
            settings.llm_base_url,
            settings.llm_api_key,
            settings.llm_model,
            settings.llm_timeout,
        ),
    )
    app.state.redis = redis
    app.state.key_store = KeyStore(pool, ttl=settings.key_cache_ttl)
    app.state.query_cache = QueryCache(redis)
    app.state.rate_limiter = RateLimiter(redis)
    log.info(
        "ready: providers=%s fallbacks=%s",
        [p.name for p in providers],
        [p.name for p in fallbacks],
    )
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
        version="0.5.0",
        summary="Open-source, self-hostable search API for AI agents.",
        lifespan=lifespan,
    )
    app.include_router(search.router)
    app.include_router(extract.router)
    app.include_router(usage.router)
    app.include_router(verify.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
app = create_app()
