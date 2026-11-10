import xml.etree.ElementTree as ET

import httpx

from sift.pipeline.models import Page

from .base import Candidate, Provider, ProviderError, parse_ts

_NS = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivProvider(Provider):
    """arXiv via its Atom API. One request per 3 seconds, enforced by a rate
    limit on the quota manager. Abstracts are the snippet and the content."""

    name = "arxiv"
    academic_only = True
    rate_limit = (1, 3)

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        try:
            resp = await self.client.get(
                "https://export.arxiv.org/api/query",
                params={"search_query": f"all:{query}", "max_results": limit},
            )
        except httpx.HTTPError as e:
            raise ProviderError(f"arxiv: {type(e).__name__}: {e}") from e
        if resp.status_code == 429:
            raise ProviderError("arxiv: rate limited upstream")
        if resp.status_code >= 400:
            raise ProviderError(f"arxiv: HTTP {resp.status_code}")
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise ProviderError(f"arxiv: non-XML response: {resp.text[:80]!r}") from e
        out = []
        for entry in root.findall("atom:entry", _NS):
            title = " ".join((entry.findtext("atom:title", "", _NS) or "").split())
            summary = " ".join((entry.findtext("atom:summary", "", _NS) or "").split())
            link = entry.findtext("atom:id", "", _NS)
            if not link:
                continue
            out.append(
                Candidate(
                    url=link,
                    title=title or link,
                    snippet=summary[:400],
                    source=self.name,
                    published=parse_ts(entry.findtext("atom:published", None, _NS)),
                    updated=parse_ts(entry.findtext("atom:updated", None, _NS)),
                    content=summary,
                    extra={
                        "authors": [
                            a.findtext("atom:name", "", _NS)
                            for a in entry.findall("atom:author", _NS)
                        ]
                    },
                )
            )
        return out

    def owns(self, url: str) -> bool:
        return url.startswith("https://arxiv.org/") or url.startswith("http://arxiv.org/")

    async def fetch_page(self, url: str) -> Page | None:
        return None
