"""The ``devdocs`` pack: developer documentation from official sources.

Each source is crawled from its official docs site (robots.txt respected, one
request per second per host) and cleaned with the live pipeline. The per-site
license table is recorded in the manifest. The full pack is ~100–300K passages;
``limit_per_source`` caps the crawl for a smaller local build.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from sift.config import get_settings
from sift.pipeline.fetch import Fetcher
from sift.pipeline.models import Page
from sift.seeds.crawler import Crawler
from sift.seeds.packs import build_pack

log = logging.getLogger(__name__)

# Official docs, one seed URL per source. The crawl stays under the seed's path.
SOURCES: list[dict] = [
    {"name": "python", "license": "PSF (zero-clause BSD)", "seed": "https://docs.python.org/3/"},
    {
        "name": "postgres",
        "license": "PostgreSQL License",
        "seed": "https://www.postgresql.org/docs/current/",
    },
    {
        "name": "mdn",
        "license": "CC BY-SA 2.5",
        "seed": "https://developer.mozilla.org/en-US/docs/Web/JavaScript",
    },
    {"name": "react", "license": "MIT (docs)", "seed": "https://react.dev/"},
    {"name": "node", "license": "MIT", "seed": "https://nodejs.org/docs/latest/api/"},
    {"name": "redis", "license": "RSAL / CC BY-SA", "seed": "https://redis.io/docs/latest/"},
    {"name": "docker", "license": "Apache-2.0", "seed": "https://docs.docker.com/"},
    {"name": "fastapi", "license": "MIT", "seed": "https://fastapi.tiangolo.com/"},
    {"name": "rails", "license": "CC BY-SA 4.0", "seed": "https://guides.rubyonrails.org/"},
    {"name": "aws", "license": "AWS docs license", "seed": "https://docs.aws.amazon.com/"},
]


async def collect_pages(
    sources: list[dict] | None = None,
    *,
    limit_per_source: int = 500,
    delay: float = 1.0,
) -> list[Page]:
    """Crawl each source's docs and return the cleaned pages (source-tagged)."""
    settings = get_settings()
    sources = sources or SOURCES
    pages: list[Page] = []
    async with httpx.AsyncClient(
        headers={"User-Agent": settings.user_agent},
        timeout=httpx.Timeout(10.0, connect=3.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=8),
    ) as client:
        fetcher = Fetcher(client, None, timeout=10.0, max_bytes=settings.fetch_max_bytes)
        crawler = Crawler(fetcher, delay=delay, max_pages=limit_per_source)
        for source in sources:
            log.info("crawling %s from %s", source["name"], source["seed"])
            count = 0
            async for page in crawler.crawl([source["seed"]], scope=source["seed"]):
                page.source = source["name"]
                pages.append(page)
                count += 1
            log.info("crawled %s: %s pages", source["name"], count)
    return pages


async def build_devdocs(out_dir: Path, embed, *, limit_per_source: int = 500, delay: float = 1.0):
    pages = await collect_pages(limit_per_source=limit_per_source, delay=delay)
    licenses = {s["name"]: s["license"] for s in SOURCES}
    manifest = await __import__("asyncio").to_thread(
        build_pack,
        pages,
        Path(out_dir),
        name="devdocs",
        license="varies per site (see licenses)",
        source=", ".join(s["name"] for s in SOURCES),
        embed=embed,
        licenses=licenses,
    )
    return manifest, len(pages)
