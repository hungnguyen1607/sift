"""Tiny, zero-dependency client for a running Sift server.

    from sift import search
    for r in search("who created Rust?")["results"]:
        print(r["url"], r["content"])

Works against any Sift instance; set ``SIFT_URL`` (default
``http://localhost:8000``) or pass ``base_url``. Stdlib only, so you can copy
this file into another project and it just works.
"""

from __future__ import annotations

import json
import os
import urllib.request

DEFAULT_URL = "http://localhost:8000"


def _base_url(base_url: str | None) -> str:
    return (base_url or os.environ.get("SIFT_URL", DEFAULT_URL)).rstrip("/")


def _post(path: str, payload: dict, base_url: str | None, timeout: float = 120) -> dict:
    req = urllib.request.Request(
        f"{_base_url(base_url)}{path}",
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def search(
    query: str,
    max_tokens: int = 1500,
    depth: str = "basic",
    max_results: int = 5,
    fresh_only: bool = False,
    base_url: str | None = None,
) -> dict:
    """Full Sift response: results (with quote offsets), providers, timings."""
    return _post(
        "/v1/search",
        {
            "query": query,
            "max_tokens": max_tokens,
            "search_depth": depth,
            "max_results": max_results,
            "fresh_only": fresh_only,
        },
        base_url,
    )


def passages(query: str, max_tokens: int = 1500, base_url: str | None = None) -> list[str]:
    """Just the passage texts, ready to drop into an LLM's context."""
    return [
        r["content"] for r in search(query, max_tokens=max_tokens, base_url=base_url)["results"]
    ]


def extract(urls: list[str], base_url: str | None = None) -> dict:
    """Clean text of the given URLs."""
    return _post("/v1/extract", {"urls": urls}, base_url)


def verify(claim: str, max_sources: int = 5, base_url: str | None = None) -> dict:
    """Check a claim against sources; returns verdict + cited quotes."""
    return _post("/v1/verify", {"claim": claim, "max_sources": max_sources}, base_url)
