"""Background worker: python -m sift.worker"""

import asyncio
import logging
import signal
import time

from sift.config import get_settings
from sift.index.db import create_pool, migrate
from sift.pipeline.embed import FastEmbedder
from sift.worker import queue
from sift.worker.jobs import HANDLERS

log = logging.getLogger("sift.worker")


async def run_once(pool, embedder) -> bool:
    """Claim and run one job. Returns False when the queue is empty."""
    async with pool.acquire() as conn:
        job = await queue.claim(conn)
        if job is None:
            return False
        handler = HANDLERS.get(job.kind)
        t0 = time.perf_counter()
        try:
            if handler is None:
                raise ValueError(f"unknown job kind {job.kind!r}")
            result = await handler(conn, embedder, job.payload)
            await queue.complete(conn, job)
            log.info("job %s %s: %s (%.1fs)", job.id, job.kind, result, time.perf_counter() - t0)
        except Exception as e:
            log.exception("job %s %s failed (attempt %s)", job.id, job.kind, job.attempts)
            await queue.fail(conn, job, f"{type(e).__name__}: {e}")
    return True


async def main() -> None:
    settings = get_settings()
    pool = await create_pool(settings.database_url)
    await migrate(pool)
    embedder = FastEmbedder(threads=settings.worker_embed_threads, cache_dir=settings.model_cache)
    await asyncio.to_thread(embedder.warmup)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    log.info("worker ready")
    last_maintain = 0.0
    while not stop.is_set():
        if time.monotonic() - last_maintain > 60:
            async with pool.acquire() as conn:
                await queue.maintain(conn)
            last_maintain = time.monotonic()
        if not await run_once(pool, embedder):
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except TimeoutError:
                pass
    await pool.close()
    embedder.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(main())
