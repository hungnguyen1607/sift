import re
from urllib.parse import urlsplit

import httpx

from sift.pipeline.htmlmd import html_to_markdown
from sift.pipeline.models import Page

from .base import Candidate, Provider, parse_ts, strip_html

# Custom API filter: the default "withbody" fields plus each question's answers
# with body, score and accepted flag. Made once with /2.3/filters/create;
# filters never expire.
ANSWERS_FILTER = "!X-cWn5YrCQCchzB5B4*yqi7.8vyuccIux5yi9KD"
MAX_ANSWERS = 3
_QID_RE = re.compile(r"^/questions/(\d+)")


class StackExchangeProvider(Provider):
    """StackExchange search through the official API.

    StackOverflow's robots.txt disallows all crawlers, so Sift never fetches its
    pages. The question and its top answers come with the search response
    (one API call), and links found by other providers use /questions/{id}.
    300 requests/day without a key, 10,000/day with one; the caps sit a little
    below that.
    """

    name = "stackexchange"
    period = "day"

    def __init__(self, client: httpx.AsyncClient, key: str | None = None, site="stackoverflow"):
        self.client = client
        self.key = key or None
        self.site = site
        self.cap = 9_500 if self.key else 280

    def _params(self, **extra) -> dict:
        params = {"site": self.site, "filter": ANSWERS_FILTER, **extra}
        if self.key:
            params["key"] = self.key
        return params

    async def search(self, query: str, limit: int) -> list[Candidate]:
        data = await self._get_json(
            self.client,
            "https://api.stackexchange.com/2.3/search/advanced",
            params=self._params(order="desc", sort="relevance", q=query, pagesize=limit),
        )
        return [self._candidate(item) for item in data.get("items", [])]

    def _candidate(self, item: dict) -> Candidate:
        title = strip_html(item.get("title"))
        return Candidate(
            url=item["link"],
            title=title,
            snippet=strip_html(item.get("body"), max_len=400),
            source=self.name,
            published=parse_ts(item.get("creation_date")),
            updated=parse_ts(item.get("last_activity_date")),
            content=self._content(title, item),
            extra={
                "score": item.get("score"),
                "is_answered": item.get("is_answered"),
                "tags": item.get("tags", []),
            },
        )

    @staticmethod
    def _content(title: str, item: dict) -> str:
        parts = [f"# {title}", html_to_markdown(item.get("body", ""))]
        answers = sorted(item.get("answers", []), key=lambda a: -a.get("score", 0))
        top = answers[:MAX_ANSWERS]
        accepted = next((a for a in answers if a.get("is_accepted")), None)
        if accepted and accepted not in top:
            top = [accepted, *top[: MAX_ANSWERS - 1]]
        for a in top:
            label = "Accepted answer" if a.get("is_accepted") else "Answer"
            parts.append(f"## {label} (score {a.get('score', 0)})")
            parts.append(html_to_markdown(a.get("body", "")))
        return "\n\n".join(p for p in parts if p)

    def owns(self, url: str) -> bool:
        parts = urlsplit(url)
        return parts.hostname in (f"{self.site}.com", f"www.{self.site}.com") and bool(
            _QID_RE.match(parts.path)
        )

    async def fetch_page(self, url: str) -> Page | None:
        qid = _QID_RE.match(urlsplit(url).path).group(1)
        data = await self._get_json(
            self.client,
            f"https://api.stackexchange.com/2.3/questions/{qid}",
            params=self._params(),
        )
        items = data.get("items", [])
        if not items:
            return None
        c = self._candidate(items[0])
        return Page(c.url, c.title, c.content, self.name, c.published, c.updated)
