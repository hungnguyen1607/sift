import re
import time

import httpx

from .base import Candidate, Provider, parse_ts, strip_html

_TTL = 3600  # the feed updates hourly and asks not to be abused
_GENERIC = frozenset("job jobs role roles position positions opening openings hiring work".split())


class ArbeitnowProvider(Provider):
    """Arbeitnow's free public job feed (no key). It has no search parameter,
    so Sift reads the latest postings once an hour and filters them locally:
    every query keyword must appear in the title, company, tags or location,
    and "remote" keeps remote postings only."""

    name = "arbeitnow"
    keyword_search = True
    URL = "https://www.arbeitnow.com/api/job-board-api"

    def __init__(self, client: httpx.AsyncClient, pages: int = 2):
        self.client = client
        self.pages = pages
        self._cache: tuple[float, list[dict]] | None = None

    async def _jobs(self) -> list[dict]:
        if self._cache and time.monotonic() - self._cache[0] < _TTL:
            return self._cache[1]
        jobs: list[dict] = []
        for page in range(1, self.pages + 1):
            data = await self._get_json(self.client, self.URL, params={"page": page})
            jobs.extend(data.get("data", []))
            if not (data.get("links") or {}).get("next"):
                break
        self._cache = (time.monotonic(), jobs)
        return jobs

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        words = [w for w in re.findall(r"[\w+#.]+", query.lower()) if len(w) > 1]
        remote = "remote" in words
        terms = [w for w in words if w not in _GENERIC and w != "remote"]
        out = []
        for j in await self._jobs():
            hay = " ".join(
                [
                    j.get("title", ""),
                    j.get("company_name", ""),
                    j.get("location", ""),
                    " ".join(j.get("tags") or []),
                    " ".join(j.get("job_types") or []),
                ]
            ).lower()
            if remote and not j.get("remote"):
                continue
            if not all(t in hay for t in terms):
                continue
            out.append(
                Candidate(
                    url=j["url"],
                    title=f"{j.get('title', '')} at {j.get('company_name', '')}".strip(),
                    snippet=strip_html(j.get("description"), max_len=300),
                    source=self.name,
                    published=parse_ts(j.get("created_at")),
                    extra={
                        "job": {
                            "title": j.get("title"),
                            "company": j.get("company_name"),
                            "location": j.get("location"),
                            "remote": bool(j.get("remote")),
                            "employment_type": ", ".join(j.get("job_types") or []) or None,
                            "tags": j.get("tags") or None,
                        }
                    },
                )
            )
            if len(out) >= limit:
                break
        return out
