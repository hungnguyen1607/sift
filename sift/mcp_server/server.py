"""Sift as an MCP server (stdio).

The server talks to the HTTP API over `SIFT_URL` / `SIFT_API_KEY` rather than
importing the pipeline, so it stays light and works against the hosted demo.

    claude mcp add sift -- python -m sift.mcp_server

Tools:
  - `search`: answer a question, returning compact numbered passages an agent
    can cite as `[n]`.
  - `extract`: return the clean Markdown of a set of URLs.
  - `verify`: check a claim against sources and report the verdict.
"""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

from sift.pipeline.pack import truncate_tokens

mcp = FastMCP(
    "sift",
    instructions=(
        "Sift is a search API for agents. Use `search` to find passages that answer "
        "a question and cite them as [n] with their URL. Use `extract` to pull the clean "
        "text of specific pages. Use `verify` to check whether a claim is supported."
    ),
)

DEFAULT_URL = "http://localhost:8000"
EXTRACT_TOKEN_LIMIT = 4000


def _client() -> httpx.AsyncClient:
    base = os.environ.get("SIFT_URL", DEFAULT_URL).rstrip("/")
    headers = {}
    if os.environ.get("SIFT_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['SIFT_API_KEY']}"
    return httpx.AsyncClient(base_url=base, headers=headers, timeout=20.0)


def _date(r: dict) -> str:
    d = r.get("published") or r.get("updated")
    return d[:10] if d else ""


async def search_impl(
    client: httpx.AsyncClient,
    query: str,
    max_tokens: int = 2000,
    depth: str = "advanced",
    include_domains: list[str] | None = None,
    fresh_only: bool = False,
) -> str:
    resp = await client.post(
        "/v1/search",
        json={
            "query": query,
            "max_tokens": max_tokens,
            "search_depth": depth,
            "include_domains": include_domains or [],
            "fresh_only": fresh_only,
        },
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("results"):
        return "No results."
    lines = []
    for i, r in enumerate(data["results"], 1):
        lines.append(f"[{i}] {r['title']} — {r['url']} ({_date(r)})")
        lines.append(f"> {r['content']}")
    info = "providers: " + ", ".join(data.get("providers_used", []))
    if data.get("cache_hit"):
        info += " (cache hit)"
    lines.append(info)
    return "\n\n".join(lines)


async def extract_impl(client: httpx.AsyncClient, urls: list[str]) -> str:
    resp = await client.post("/v1/extract", json={"urls": urls})
    resp.raise_for_status()
    data = resp.json()
    out: list[str] = []
    for r in data.get("results", []):
        text = truncate_tokens(r["raw_content"], EXTRACT_TOKEN_LIMIT)
        out.append(f"# {r['title']} ({r['url']})\n\n{text}")
    for f in data.get("failed_results", []):
        out.append(f"## failed: {f['url']} — {f['error']}")
    return "\n\n---\n\n".join(out) if out else "Nothing extracted."


async def verify_impl(client: httpx.AsyncClient, claim: str, max_sources: int = 5) -> str:
    resp = await client.post("/v1/verify", json={"claim": claim, "max_sources": max_sources})
    resp.raise_for_status()
    data = resp.json()
    lines = [f"Verdict: {data['verdict']} (confidence {data['confidence']})"]
    for group in ("supporting", "contradicting"):
        for ev in data.get(group, []):
            lines.append(f"[{group}] {ev['quote']!r} — {ev['url']}")
    return "\n".join(lines)


@mcp.tool()
async def search(
    query: str,
    max_tokens: int = 2000,
    depth: str = "advanced",
    include_domains: list[str] | None = None,
    fresh_only: bool = False,
) -> str:
    """Search the web for passages that answer `query`. Cite results as [n] with
    their URL. Set `max_tokens` to bound the total returned text; use
    `fresh_only` for recent information only."""
    async with _client() as client:
        return await search_impl(client, query, max_tokens, depth, include_domains, fresh_only)


@mcp.tool()
async def extract(urls: list[str]) -> str:
    """Return the clean Markdown of each URL in `urls` (code blocks and tables
    kept). Use this to read a specific page an answer points to."""
    async with _client() as client:
        return await extract_impl(client, urls)


@mcp.tool()
async def verify(claim: str, max_sources: int = 5) -> str:
    """Check whether `claim` is supported or contradicted by sources, and return
    the exact supporting/contradicting quotes."""
    async with _client() as client:
        return await verify_impl(client, claim, max_sources)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
