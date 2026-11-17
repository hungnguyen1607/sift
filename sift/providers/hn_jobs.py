import re
import time

import httpx

from .base import Candidate, Provider, parse_ts, strip_html

_STORY_TTL = 6 * 3600
_GENERIC = frozenset("job jobs role roles position positions opening openings hiring".split())
# Job seekers sometimes post in the hiring thread ("Location: … Remote: yes …
# Résumé: …", the "Who wants to be hired?" template).
_SEEKER = re.compile(r"(?i)^(location|seeking|looking for)\s*:|\b(r[ée]sum[ée]|cv)\s*:")


class HNJobsProvider(Provider):
    """Top-level comments of the latest "Ask HN: Who is hiring?" thread, via
    the free Algolia API. By convention each starts with a header line like
    ``Company | Role | Location | REMOTE | $salary``, which becomes the title."""

    name = "hn_jobs"
    keyword_search = True
    API = "https://hn.algolia.com/api/v1"

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self._story: tuple[float, str, str] | None = None  # (fetched, id, title)

    async def _latest_story(self) -> tuple[str, str] | None:
        if self._story and time.monotonic() - self._story[0] < _STORY_TTL:
            return self._story[1], self._story[2]
        data = await self._get_json(
            self.client,
            f"{self.API}/search_by_date",
            params={"tags": "story,author_whoishiring", "hitsPerPage": 5},
        )
        for hit in data.get("hits", []):
            if "who is hiring" in (hit.get("title") or "").lower():
                self._story = (time.monotonic(), hit["objectID"], hit["title"])
                return hit["objectID"], hit["title"]
        return None

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        story = await self._latest_story()
        if story is None:
            return []
        story_id, story_title = story
        terms = [w for w in query.split() if w.lower() not in _GENERIC]
        data = await self._get_json(
            self.client,
            f"{self.API}/search",
            params={
                "query": " ".join(terms),
                "tags": f"comment,story_{story_id}",
                "hitsPerPage": min(limit * 2, 100),
            },
        )
        out = []
        for hit in data.get("hits", []):
            if str(hit.get("parent_id")) != story_id:  # replies, not postings
                continue
            text = hit.get("comment_text") or ""
            first = re.split(r"<p>|\n", text, maxsplit=1)[0]
            header = strip_html(first, max_len=160)
            body = strip_html(text, max_len=400)
            if _SEEKER.search(header) or _SEEKER.search(body[:300]):
                continue
            parts = [p.strip() for p in header.split("|")]
            out.append(
                Candidate(
                    url=f"https://news.ycombinator.com/item?id={hit['objectID']}",
                    title=header or story_title,
                    snippet=body,
                    source=self.name,
                    published=parse_ts(hit.get("created_at")),
                    extra={
                        "job": {
                            "company": parts[0] if len(parts) > 1 else None,
                            "remote": True if re.search(r"\bremote\b", header, re.I) else None,
                            "thread": story_title,
                        }
                    },
                )
            )
            if len(out) >= limit:
                break
        return out
