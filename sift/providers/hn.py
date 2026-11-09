from urllib.parse import parse_qs, urlsplit

import httpx

from sift.pipeline.htmlmd import html_to_markdown
from sift.pipeline.models import Page

from .base import Candidate, Provider, parse_ts, strip_html

MAX_COMMENTS = 12


class HackerNewsProvider(Provider):
    """Hacker News stories via the free Algolia API. Discussion pages are read
    through Algolia's items endpoint (story text + top comments) rather than
    scraped, because the HTML is mostly navigation."""

    name = "hn"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def search(self, query: str, limit: int) -> list[Candidate]:
        data = await self._get_json(
            self.client,
            "https://hn.algolia.com/api/v1/search",
            params={"query": query, "tags": "story", "hitsPerPage": limit},
        )
        out = []
        for hit in data.get("hits", []):
            item_url = f"https://news.ycombinator.com/item?id={hit['objectID']}"
            title = hit.get("title") or hit.get("story_title")
            if not title:
                continue
            out.append(
                Candidate(
                    # Link posts point at the article; Ask/Show HN have no URL.
                    url=hit.get("url") or item_url,
                    title=strip_html(title),
                    snippet=strip_html(hit.get("story_text"), max_len=400),
                    source=self.name,
                    published=parse_ts(hit.get("created_at")),
                    extra={"points": hit.get("points"), "discussion_url": item_url},
                )
            )
        return out

    def owns(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.hostname == "news.ycombinator.com" and parts.path == "/item"

    async def fetch_page(self, url: str) -> Page | None:
        item_id = (parse_qs(urlsplit(url).query).get("id") or [""])[0]
        if not item_id.isdigit():
            return None
        item = await self._get_json(self.client, f"https://hn.algolia.com/api/v1/items/{item_id}")
        title = strip_html(item.get("title")) or f"HN item {item_id}"
        parts = [f"# {title}"]
        if item.get("url"):
            parts.append(f"Link: {item['url']}")
        if item.get("text"):
            parts.append(html_to_markdown(item["text"]))
        comments = [c for c in item.get("children", []) if c.get("text")][:MAX_COMMENTS]
        if comments:
            parts.append("## Comments")
            for c in comments:
                parts.append(f"**{c.get('author') or 'anonymous'}:** {html_to_markdown(c['text'])}")
        return Page(
            url=url,
            title=title,
            text="\n\n".join(parts),
            source=self.name,
            published=parse_ts(item.get("created_at")),
        )
