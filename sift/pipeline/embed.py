"""bge-small-en-v1.5 embeddings via fastembed (ONNX, CPU)."""

from __future__ import annotations

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor
from typing import Protocol

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
# BGE's recommended instruction for short queries against passages.
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


class Embedder(Protocol):
    async def embed_query(self, query: str) -> list[float]: ...
    async def embed_passages(self, texts: list[str]) -> list[list[float]]: ...
    def estimate_ms(self, texts: list[str]) -> float: ...


def embedding_input(title: str, heading: str | None, text: str, max_words: int | None = None):
    """What gets embedded for a passage: page title and section give context."""
    if max_words is not None:
        words = text.split()
        if len(words) > max_words:
            text = " ".join(words[:max_words])
    head = f"{title} — {heading}" if heading and heading != title else title
    return f"{head}\n{text}" if head else text


def normalize(vec) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [float(x) / norm for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))  # both are unit vectors


class FastEmbedder:
    """Loads the model lazily. ONNX sessions are thread-safe, so a short query
    never waits behind a big passage batch: there are two threads, and passage
    batches take turns through a lock."""

    def __init__(self, threads: int = 2, cache_dir: str | None = None):
        self.threads = threads
        self.cache_dir = cache_dir
        self._model = None
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="embed")
        self._batch_lock = asyncio.Lock()
        # Measured cost per word, so callers can fit work into a time budget.
        # Starts pessimistic (about 1.5 ms/word on a busy 2-core box).
        self.ms_per_word = 1.5

    def estimate_ms(self, texts: list[str]) -> float:
        return self.ms_per_word * sum(len(t.split()) for t in texts)

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            self._model = TextEmbedding(MODEL_NAME, threads=self.threads, cache_dir=self.cache_dir)
        return self._model

    def warmup(self) -> None:
        list(self._load().embed(["warmup"]))

    def _embed_sync(self, texts: list[str]) -> list[list[float]]:
        import time

        t0 = time.perf_counter()
        out = [normalize(v) for v in self._load().embed(texts, batch_size=16)]
        words = sum(len(t.split()) for t in texts)
        if words >= 20:
            observed = (time.perf_counter() - t0) * 1000 / words
            self.ms_per_word = 0.7 * self.ms_per_word + 0.3 * observed
        return out

    async def embed_query(self, query: str) -> list[float]:
        loop = asyncio.get_running_loop()
        [vec] = await loop.run_in_executor(self._pool, self._embed_sync, [QUERY_PREFIX + query])
        return vec

    async def embed_passages(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        async with self._batch_lock:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(self._pool, self._embed_sync, texts)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
