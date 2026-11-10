from urllib.parse import quote, unquote, urlsplit

import httpx

from sift.pipeline.clean import wiki_extract_to_markdown
from sift.pipeline.models import Page

from .base import Candidate, Provider, parse_ts, strip_html


class WikipediaProvider(Provider):
    """MediaWiki search plus plain-text extracts for page content.

    Reading articles through the API is faster than scraping and cleaning
    the 1 MB HTML pages, and it's what Wikipedia asks bots to do.
    """

    name = "wikipedia"

    def __init__(self, client: httpx.AsyncClient, lang: str = "en"):
        self.client = client
        self.lang = lang
        self.host = f"{lang}.wikipedia.org"
        self.api = f"https://{self.host}/w/api.php"

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        data = await self._get_json(
            self.client,
            self.api,
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "srlimit": limit,
                "srprop": "snippet|timestamp",
                "format": "json",
                "formatversion": 2,
            },
        )
        out = []
        for hit in data.get("query", {}).get("search", []):
            title = hit["title"]
            out.append(
                Candidate(
                    url=f"https://{self.host}/wiki/{quote(title.replace(' ', '_'))}",
                    title=title,
                    snippet=strip_html(hit.get("snippet")),
                    source=self.name,
                    # The API only exposes the last-edit time, not creation.
                    updated=parse_ts(hit.get("timestamp")),
                )
            )
        return out

    def owns(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.hostname == self.host and parts.path.startswith("/wiki/")

    async def fetch_page(self, url: str) -> Page | None:
        title = unquote(urlsplit(url).path[len("/wiki/") :]).replace("_", " ")
        data = await self._get_json(
            self.client,
            self.api,
            params={
                "action": "query",
                "prop": "extracts|revisions",
                "explaintext": 1,
                "exsectionformat": "wiki",
                "rvprop": "timestamp",
                "titles": title,
                "redirects": 1,
                "format": "json",
                "formatversion": 2,
            },
        )
        pages = data.get("query", {}).get("pages", [])
        if not pages or pages[0].get("missing") or not pages[0].get("extract"):
            return None
        p = pages[0]
        revs = p.get("revisions") or [{}]
        return Page(
            url=f"https://{self.host}/wiki/{quote(p['title'].replace(' ', '_'))}",
            title=p["title"],
            text=wiki_extract_to_markdown(p["extract"]),
            source=self.name,
            updated=parse_ts(revs[0].get("timestamp")),
        )
