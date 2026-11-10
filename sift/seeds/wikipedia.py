"""The ``wikipedia-200k`` pack: the most-read English Wikipedia articles.

Articles are ranked by pageviews (Wikimedia's top-pages endpoint, summed over
the last 12 months, minus `Main_Page` and special pages), then their clean text
is pulled from the Hugging Face ``wikimedia/wikipedia`` dataset and capped at
~8 passages per article so the ~200K articles stay ~1.2–1.6M passages.

The full build needs the dataset (~20 GB download) and a GPU; the ranking and
capping logic below is unit-testable without either.
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from sift.pipeline.models import Page
from sift.seeds.packs import build_pack

log = logging.getLogger(__name__)

MAX_PASSAGES_PER_ARTICLE = 8
_PAGEVIEWS_TOP = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
    "en.wikipedia.org/all-access/{year}/{month:02d}/all-days"
)


def rank_titles(monthly: list[list[str]]) -> list[str]:
    """Merge monthly top-article lists into one ranking: score each title by
    1/(rank+1) summed across months (Reciprocal Rank style)."""
    score: Counter[str] = Counter()
    for month in monthly:
        for rank, title in enumerate(month, start=1):
            score[title] += 1.0 / (rank + 1)
    return [t for t, _ in score.most_common()]


async def top_titles(client, months: int = 12, top_per_month: int = 1000) -> list[str]:
    """Fetch the top-1000 articles per month for the last ``months`` months and
    merge them into a single ranked list."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    monthly: list[list[str]] = []
    for i in range(months):
        year, month = divmod(now.year * 12 + now.month - 1 - i, 12)
        month += 1
        resp = await client.get(_PAGEVIEWS_TOP.format(year=year, month=month))
        if resp.status_code != 200:
            continue
        items = resp.json().get("items", [])[0].get("articles", [])
        titles = [
            a["article"]
            for a in items[:top_per_month]
            if not a["article"].startswith(("Special:", "Main_Page"))
        ]
        monthly.append(titles)
    return rank_titles(monthly)


def _pages_from_dataset(ds, titles: set[str]) -> list[Page]:
    pages = []
    for row in ds:
        title = row.get("title")
        if title not in titles:
            continue
        text = row.get("text") or ""
        if len(text.split()) < 40:
            continue
        pages.append(
            Page(
                url=f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                title=title,
                text=text,
                source="wikipedia",
            )
        )
    return pages


async def build_wikipedia(
    out_dir: Path,
    embed,
    *,
    top_k: int = 200_000,
    max_passages_per_article: int = MAX_PASSAGES_PER_ARTICLE,
):
    """Build the pack. Requires ``datasets`` and network; call on a GPU box."""
    import httpx

    async with httpx.AsyncClient(follow_redirects=True) as client:
        titles = await top_titles(client)
    titles = titles[:top_k]
    log.info("ranked %s articles", len(titles))

    from datasets import load_dataset  # lazy: heavy, only for the real build

    ds = load_dataset("wikimedia/wikipedia", "20231101.en", split="train", streaming=True)
    pages = await __import__("asyncio").to_thread(_pages_from_dataset, ds, set(titles))
    log.info("collected %s article texts", len(pages))
    manifest = await __import__("asyncio").to_thread(
        build_pack,
        pages,
        Path(out_dir),
        name="wikipedia-200k",
        license="CC BY-SA",
        source="Wikimedia (top articles by pageviews)",
        embed=embed,
        max_passages_per_page=max_passages_per_article,
    )
    return manifest, len(pages)
