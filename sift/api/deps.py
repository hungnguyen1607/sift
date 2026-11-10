from __future__ import annotations

import asyncio
import logging
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Annotated, Protocol

from fastapi import Depends, Request

from sift.config import Settings
from sift.pipeline.embed import Embedder
from sift.pipeline.fetch import Fetcher
from sift.pipeline.models import Page, Passage
from sift.providers import Provider, QuotaManager

log = logging.getLogger(__name__)


class Index(Protocol):
    async def search(self, query: str, query_vec, limit: int) -> list[Passage]: ...
    async def known_pages(self, urls: list[str]) -> dict[str, list[Passage]]: ...
    async def page_texts(self, urls: list[str]) -> dict[str, str]: ...


class Enqueuer(Protocol):
    async def __call__(self, kind: str, payload: dict) -> object: ...


@dataclass
class Engine:
    """Everything a request needs, built once at startup."""

    settings: Settings
    providers: list[Provider]
    quota: QuotaManager
    fallbacks: list[Provider] = field(default_factory=list)
    fetcher: Fetcher | None = None
    embedder: Embedder | None = None
    index: Index | None = None
    enqueue: Enqueuer | None = None
    clean_executor: Executor | None = None
    page_cache: object | None = None
    reranker: object | None = None
    llm: object | None = None
    _bg: set = field(default_factory=set)

    def schedule_index(self, pages: list[Page]) -> None:
        """Queue pages for the index writer without making the request wait."""
        if self.enqueue is None or not pages:
            return
        from sift.worker.jobs import page_payload

        async def run():
            for page in pages:
                try:
                    await self.enqueue("index_page", page_payload(page))
                except Exception as e:
                    log.warning("could not enqueue %s: %r", page.url, e)

        task = asyncio.create_task(run())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)


def get_engine(request: Request) -> Engine:
    return request.app.state.engine


EngineDep = Annotated[Engine, Depends(get_engine)]
