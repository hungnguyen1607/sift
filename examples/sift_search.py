"""A LangChain-compatible Sift search tool.

Tavily's own clients hard-code ``https://api.tavily.com`` and don't accept a
base URL, so this thin tool calls Sift's API directly. It is a LangChain
``BaseTool`` when LangChain is installed, and otherwise a plain class with a
``run`` method, so it's usable anywhere.
"""

from __future__ import annotations

import httpx

try:
    from langchain_core.tools import BaseTool
except ImportError:  # LangChain optional
    BaseTool = object  # type: ignore[assignment,misc]


def _format(results: list[dict]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r['title']} — {r['url']}")
        lines.append(f"> {r['content']}")
    return "\n\n".join(lines) or "No results."


def search_sift(
    query: str,
    max_tokens: int = 1500,
    base_url: str = "http://localhost:8000",
    api_key: str | None = None,
) -> str:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = httpx.post(
        f"{base_url.rstrip('/')}/v1/search",
        json={"query": query, "max_tokens": max_tokens},
        headers=headers,
        timeout=30.0,
    )
    resp.raise_for_status()
    return _format(resp.json()["results"])


class SiftSearch(BaseTool):
    """Search the web through Sift and return citable passages.

    Use this when you need up-to-date or factual information. Results are
    numbered [n]; cite them with their URL.
    """

    name: str = "sift_search"
    description: str = (
        "Search the web for information that answers a question. Returns passages "
        "with their URLs; cite them as [n] with the URL."
    )
    max_tokens: int = 1500
    base_url: str = "http://localhost:8000"
    api_key: str | None = None

    def _run(self, query: str, **kwargs) -> str:  # noqa: ANN003
        return search_sift(query, self.max_tokens, self.base_url, self.api_key)

    async def _arun(self, query: str, **kwargs) -> str:  # noqa: ANN003
        return self._run(query, **kwargs)
