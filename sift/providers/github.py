import re
from urllib.parse import urlsplit

import httpx

from sift.pipeline.models import Page

from .base import Candidate, Provider, parse_ts, strip_html

_REPO_RE = re.compile(r"^/([^/]+)/([^/]+)")


class GitHubProvider(Provider):
    """GitHub repository search plus READMEs read through the API.

    The README endpoint returns raw Markdown (with code blocks intact), so the
    content never needs scraping. Search allows 30 requests/min with a token.
    """

    name = "github"
    period = "minute"

    def __init__(self, client: httpx.AsyncClient, token: str | None = None):
        self.client = client
        self.token = token
        self.cap = 30 if self.token else None

    def _headers(self) -> dict:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        data = await self._get_json(
            self.client,
            "https://api.github.com/search/repositories",
            params={"q": query, "per_page": limit},
            headers=self._headers(),
        )
        out = []
        for item in data.get("items", []):
            url = item.get("html_url")
            if not url:
                continue
            out.append(
                Candidate(
                    url=url,
                    title=item.get("full_name") or url,
                    snippet=strip_html(item.get("description"), max_len=400),
                    source=self.name,
                    published=parse_ts(item.get("created_at")),
                    updated=parse_ts(item.get("pushed_at")),
                    extra={
                        "stars": item.get("stargazers_count"),
                        "language": item.get("language"),
                        "default_branch": item.get("default_branch"),
                    },
                )
            )
        return out

    def owns(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.hostname == "github.com" and bool(_REPO_RE.match(parts.path))

    async def fetch_page(self, url: str) -> Page | None:
        m = _REPO_RE.match(urlsplit(url).path)
        if not m:
            return None
        owner, repo = m.group(1), m.group(2)
        headers = {"Accept": "application/vnd.github.raw"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            resp = await self.client.get(
                f"https://api.github.com/repos/{owner}/{repo}/readme", headers=headers
            )
        except httpx.HTTPError:
            return None
        if resp.status_code >= 400 or not resp.text.strip():
            return None
        title = f"{owner}/{repo}"
        return Page(
            url=f"https://github.com/{owner}/{repo}",
            title=title,
            text=f"# {title} README\n\n{resp.text}",
            source=self.name,
        )
