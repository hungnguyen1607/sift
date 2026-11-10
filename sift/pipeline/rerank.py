"""Cross-encoder reranking for advanced searches (bge/ms-marco rerankers via
fastembed, ONNX, CPU).

Loaded lazily in the API process only, with its own executor. The top passages
are rescored best-first within a time budget; whatever finishes is used, and
the rest keep their RRF order.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)

DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"
_BATCH = 8


class Reranker:
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        threads: int = 1,
        cache_dir: str | None = None,
    ):
        self.model_name = model_name
        self.cache_dir = cache_dir
        self.threads = threads
        self._model = None
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rerank")

    def _load(self):
        if self._model is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            self._model = TextCrossEncoder(
                self.model_name, threads=self.threads, cache_dir=self.cache_dir
            )
        return self._model

    def _rerank_sync(self, query: str, docs: list[str]) -> list[float]:
        return [float(s) for s in self._load().rerank(query, docs, batch_size=len(docs))]

    async def rerank(self, query: str, docs: list[str], budget_s: float) -> list[float | None]:
        """Rescore ``docs`` best-first, within ``budget_s``. Returns one score
        per doc, or None where the budget ran out before it was scored."""
        scores: list[float | None] = [None] * len(docs)
        deadline = time.perf_counter() + budget_s
        for start in range(0, len(docs), _BATCH):
            batch = docs[start : start + _BATCH]
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            try:
                got = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        self._pool, self._rerank_sync, query, batch
                    ),
                    max(remaining, 0.05),
                )
            except (TimeoutError, Exception) as e:  # model load can raise too
                log.info("rerank: %s", e)
                break
            scores[start : start + _BATCH] = got
        return scores

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
