import asyncio
import re

import httpx

from .base import Candidate, Provider

# Words for GitHub's language: qualifier ("rust developers" → language:rust).
_LANGUAGES = {
    "python": "python",
    "rust": "rust",
    "go": "go",
    "golang": "go",
    "java": "java",
    "kotlin": "kotlin",
    "swift": "swift",
    "ruby": "ruby",
    "php": "php",
    "scala": "scala",
    "haskell": "haskell",
    "elixir": "elixir",
    "erlang": "erlang",
    "clojure": "clojure",
    "javascript": "javascript",
    "js": "javascript",
    "typescript": "typescript",
    "ts": "typescript",
    "c++": "c++",
    "cpp": "c++",
    "c#": "c#",
    "csharp": "c#",
    "dart": "dart",
    "julia": "julia",
    "lua": "lua",
    "zig": "zig",
    "ocaml": "ocaml",
    "r": "r",
    "perl": "perl",
    "nim": "nim",
    "solidity": "solidity",
}
_LOCATION = re.compile(r"(?i)\b(?:in|from|based in|located in|near)\s+([a-z][\w .'-]*?)\s*$")
_DROP = frozenset(
    "github profiles profile users user developers developer devs dev engineers engineer "
    "programmers programmer people find list show me of who are on the for top best".split()
)


def to_github_query(query: str) -> tuple[str, dict]:
    """ "rust developers in berlin" → ("location:berlin language:rust", {...})."""
    q = re.sub(
        r"(?i)\bsite:\S+|\b(on|from)\s+github\b|\bgithub\b|\bwho\s+(write|use|code in)\b",
        " ",
        query,
    )
    q = " ".join(q.split())
    q = re.sub(r"^(?:find|list|show|get)\s+(?:me\s+)?\d*\s*", "", q.strip(), flags=re.I)
    quals: dict[str, str] = {}
    m = _LOCATION.search(q)
    if m:
        quals["location"] = m.group(1).strip()
        q = q[: m.start()]
    words = []
    for w in re.findall(r"[\w+#.]+", q.lower()):
        if w in _LANGUAGES and "language" not in quals:
            quals["language"] = _LANGUAGES[w]
        elif w not in _DROP and len(w) > 1:
            words.append(w)
    parts = [*words]
    for k, v in quals.items():
        parts.append(f'{k}:"{v}"' if " " in v else f"{k}:{v}")
    return " ".join(parts), quals


class GitHubUsersProvider(Provider):
    """GitHub's user search API: people by location and language, which web
    search engines barely index. Free without a key (10 searches/min); with
    ``SIFT_GITHUB_TOKEN`` it also reads each user's name, bio and followers."""

    name = "github_users"
    keyword_search = False
    period = "minute"
    API = "https://api.github.com"

    def __init__(self, client: httpx.AsyncClient, token: str | None = None):
        self.client = client
        self.token = token
        self.cap = 30 if token else 10

    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    async def search(
        self, query: str, limit: int, time_range: str | None = None
    ) -> list[Candidate]:
        q, quals = to_github_query(query)
        if not q:
            return []
        data = await self._get_json(
            self.client,
            f"{self.API}/search/users",
            params={"q": q, "per_page": min(limit, 100), "sort": "followers"},
            headers=self._headers(),
        )
        items = data.get("items", [])[:limit]
        details: list[dict] = [{}] * len(items)
        if self.token and items:  # details cost one call each: only with a token
            got = await asyncio.gather(
                *(
                    self._get_json(self.client, i["url"], headers=self._headers())
                    for i in items[:10]
                ),
                return_exceptions=True,
            )
            details = [g if isinstance(g, dict) else {} for g in got] + [{}] * (
                len(items) - len(got)
            )
        out = []
        for item, d in zip(items, details, strict=True):
            login = item["login"]
            desc = [p for p in (quals.get("language"), quals.get("location")) if p]
            snippet = d.get("bio") or (
                "GitHub user · " + " · ".join(desc) if desc else "GitHub user"
            )
            out.append(
                Candidate(
                    url=item["html_url"],
                    title=f"{d['name']} ({login})" if d.get("name") else login,
                    snippet=snippet,
                    source=self.name,
                    extra={
                        "person": {
                            "handle": login,
                            "name": d.get("name"),
                            "location": d.get("location") or quals.get("location"),
                            "language": quals.get("language"),
                            "company": d.get("company"),
                            "followers": d.get("followers"),
                            "type": item.get("type"),
                        }
                    },
                )
            )
        return out
