"""Run the evaluation: query Sift (basic + advanced) and Tavily, save raw
responses under ``eval/runs/<date>/`` so judging can be re-run without
re-spending Tavily credits.

    python eval/run.py --systems sift_basic,sift_advanced,tavily

Env: ``SIFT_URL`` (default http://localhost:8000), ``SIFT_API_KEY`` (optional),
``TAVILY_API_KEY`` (required for the tavily system). Throttled to stay inside
free-tier rate limits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

import httpx

HERE = Path(__file__).parent
QUERIES = HERE / "queries.jsonl"

SYSTEMS = {
    "sift_basic": {"type": "sift", "depth": "basic"},
    "sift_advanced": {"type": "sift", "depth": "advanced"},
    "tavily": {"type": "tavily"},
}


def load_queries() -> list[dict]:
    return [json.loads(line) for line in QUERIES.read_text().splitlines() if line.strip()]


async def run_sift(client: httpx.AsyncClient, query: str, depth: str) -> dict:
    base = os.environ.get("SIFT_URL", "http://localhost:8000").rstrip("/")
    headers = (
        {"Authorization": f"Bearer {os.environ['SIFT_API_KEY']}"}
        if os.environ.get("SIFT_API_KEY")
        else {}
    )
    resp = await client.post(
        f"{base}/v1/search",
        json={"query": query, "max_tokens": 1500, "search_depth": depth},
        headers=headers,
    )
    resp.raise_for_status()
    return resp.json()


async def run_tavily(client: httpx.AsyncClient, query: str) -> dict:
    resp = await client.post(
        "https://api.tavily.com/search",
        json={"api_key": os.environ["TAVILY_API_KEY"], "query": query, "max_results": 5},
    )
    resp.raise_for_status()
    return resp.json()


async def run_system(
    client: httpx.AsyncClient, system: str, cfg: dict, queries: list[dict], out: Path
) -> None:
    results = []
    with out.open("w") as f:
        for q in queries:
            try:
                if cfg["type"] == "sift":
                    resp = await run_sift(client, q["query"], cfg["depth"])
                    tokens = resp.get("tokens_used")
                else:
                    resp = await run_tavily(client, q["query"])
                    tokens = None
                record = {
                    "query_id": q["id"],
                    "category": q["category"],
                    "query": q["query"],
                    "system": system,
                    "response_time": resp.get("response_time"),
                    "tokens_used": tokens,
                    "results": [
                        {"title": r.get("title"), "url": r.get("url"), "content": r.get("content")}
                        for r in resp.get("results", [])
                    ],
                }
            except Exception as e:  # keep going on a per-query failure
                record = {"query_id": q["id"], "system": system, "error": str(e), "results": []}
            f.write(json.dumps(record, default=str) + "\n")
            results.append(record)
            await asyncio.sleep(1.0)  # stay under rate limits
    print(f"{system}: {len(results)} queries → {out}")


async def main(systems: list[str]) -> None:
    queries = load_queries()
    run_dir = HERE / "runs" / datetime.now().strftime("%Y-%m-%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for system in systems:
            cfg = SYSTEMS[system]
            if cfg["type"] == "tavily" and not os.environ.get("TAVILY_API_KEY"):
                print("skipping tavily: TAVILY_API_KEY not set")
                continue
            await run_system(client, system, cfg, queries, run_dir / f"{system}.jsonl")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--systems", default="sift_basic,sift_advanced,tavily")
    args = ap.parse_args()
    asyncio.run(main(args.systems.split(",")))
