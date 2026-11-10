"""The `sift` CLI: seed, keys, stats, search and doctor.

Runs inside the container (`docker compose exec api sift …`) where Postgres,
Redis and the API are reachable. `sift search` talks to the HTTP API; the rest
use the database and Redis directly.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

from sift.api.auth import new_key
from sift.config import get_settings
from sift.index.db import create_pool, migrate
from sift.seeds.packs import PACKS


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sift", description="Sift control CLI")
    sub = p.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed")
    seed_sub = seed.add_subparsers(dest="action", required=True)
    seed_sub.add_parser("list")
    seed_sub.add_parser("status")
    load = seed_sub.add_parser("load")
    load.add_argument("pack")
    load.add_argument("directory", help="local pack directory")

    keys = sub.add_parser("keys")
    keys_sub = keys.add_subparsers(dest="action", required=True)
    keys_sub.add_parser("list")
    kcreate = keys_sub.add_parser("create")
    kcreate.add_argument("--name", required=True)
    krevoke = keys_sub.add_parser("revoke")
    krevoke.add_argument("prefix")

    sub.add_parser("stats")

    search = sub.add_parser("search")
    search.add_argument("query")
    search.add_argument("--json", action="store_true", dest="as_json")
    search.add_argument("--depth", default="basic")
    search.add_argument("--max-tokens", type=int, default=1500)

    sub.add_parser("doctor")
    return p


async def _seed_list() -> None:
    print(f"{'pack':<18} {'passages':<18} description")
    for name, info in PACKS.items():
        print(f"{name:<18} {info['size']:<18} {info['source']}")


async def _seed_status(pool) -> None:
    rows = await pool.fetch(
        "SELECT name, version, passage_count, loaded_at FROM seed_packs ORDER BY name"
    )
    if not rows:
        print("no packs loaded")
        return
    for r in rows:
        print(
            f"{r['name']} v{r['version']}  {r['passage_count']} passages  loaded {r['loaded_at']}"
        )


async def _seed_load(pool, pack: str, directory: str) -> None:
    from sift.seeds.load import load_pack

    result = await load_pack(pool, directory)
    await pool.execute(
        "INSERT INTO seed_packs (name, version, passage_count) VALUES ($1, 1, $2) "
        "ON CONFLICT (name) DO UPDATE SET version = 1, passage_count = EXCLUDED.passage_count, "
        "loaded_at = now()",
        pack,
        result["passages"],
    )
    print(f"loaded {result['passages']} passages from {directory}")


async def _keys_create(pool, name: str) -> None:
    full, prefix, digest = new_key()
    await pool.execute(
        "INSERT INTO api_keys (name, prefix, key_hash) VALUES ($1, $2, $3)", name, prefix, digest
    )
    print(full)


async def _keys_list(pool) -> None:
    rows = await pool.fetch(
        "SELECT id, name, prefix, created_at, revoked_at, rpm_limit FROM api_keys ORDER BY id"
    )
    for r in rows:
        state = "revoked" if r["revoked_at"] else "active"
        print(f"{r['prefix']}  {r['name']:<20} {state}  rpm={r['rpm_limit']}")


async def _keys_revoke(pool, prefix: str) -> None:
    updated = await pool.execute(
        "UPDATE api_keys SET revoked_at = now() WHERE prefix = $1 AND revoked_at IS NULL", prefix
    )
    print(f"revoked {updated}")


async def _stats(pool, redis) -> None:
    pages = await pool.fetchval("SELECT count(*) FROM pages")
    passages = await pool.fetchval("SELECT count(*) FROM passages")
    queued = await pool.fetchval("SELECT count(*) FROM jobs WHERE status = 'queued'")
    running = await pool.fetchval("SELECT count(*) FROM jobs WHERE status = 'running'")
    lag = await pool.fetchval(
        "SELECT coalesce(extract(epoch FROM now() - min(created_at)), 0)::int "
        "FROM jobs WHERE status = 'queued'"
    )
    hits = int(await redis.get("sift:cache:hits") or 0)
    misses = int(await redis.get("sift:cache:misses") or 0)
    print(f"pages:     {pages}")
    print(f"passages:  {passages}")
    print(f"queue:     {queued} queued, {running} running, oldest {lag}s")
    total = hits + misses
    rate = f"{100 * hits / total:.1f}%" if total else "n/a"
    print(f"cache:     {hits} hits / {misses} misses ({rate})")


async def _search(query: str, depth: str, max_tokens: int, as_json: bool) -> None:
    base = os.environ.get("SIFT_URL", "http://localhost:8000").rstrip("/")
    async with httpx.AsyncClient(base_url=base, timeout=60.0) as client:
        resp = await client.post(
            "/v1/search", json={"query": query, "search_depth": depth, "max_tokens": max_tokens}
        )
        resp.raise_for_status()
    data = resp.json()
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    print(f"query: {query}  ({data['response_time']}s, cache_hit={data['cache_hit']})")
    for i, r in enumerate(data["results"], 1):
        print(f"[{i}] {r['title']} — {r['url']}")
        print(f"    {r['content'][:200]}")
    print(f"providers: {', '.join(data['providers_used'])}")


async def _doctor(pool, redis, http) -> None:
    ok = True

    def check(label: str, good: bool, detail: str = "", required: bool = True):
        nonlocal ok
        if required:
            ok = ok and good
        mark = "✓" if good else ("✗" if required else "·")
        print(f"{mark} {label}" + (f"  {detail}" if detail else ""))

    try:
        await pool.fetchval("SELECT 1")
        check("database", True)
    except Exception as e:
        check("database", False, str(e))

    try:
        await redis.ping()
        check("redis", True)
    except Exception as e:
        check("redis", False, str(e))

    settings = get_settings()
    keys = {
        "stackexchange": settings.stackexchange_key,
        "github": settings.github_token,
        "brave": settings.brave_key,
        "tavily": settings.tavily_key,
        "serper": settings.serper_key,
    }
    for name, key in keys.items():
        # Optional: absent keys just mean that source is disabled, not a failure.
        check(
            f"provider key: {name}",
            bool(key),
            "set" if key else "not set (disabled)",
            required=False,
        )

    check(
        "LLM",
        bool(settings.llm_model),
        f"{settings.llm_provider or 'openai'} {settings.llm_model}"
        if settings.llm_model
        else "off (optional)",
        required=False,
    )

    for host in ("https://en.wikipedia.org", "https://api.openalex.org"):
        try:
            await http.get(host, timeout=3.0)
            check(f"outbound: {host}", True)
        except Exception as e:
            check(f"outbound: {host}", False, str(e))

    if settings.searxng_url:
        try:
            resp = await http.get(
                f"{settings.searxng_url}/search",
                params={"q": "test", "format": "json"},
                timeout=5.0,
            )
            check("searxng", resp.status_code == 200, f"{settings.searxng_url}")
        except Exception as e:
            check("searxng", False, f"{settings.searxng_url}: {e}")
    else:
        check("searxng", False, "not configured (set SIFT_SEARXNG_URL)")

    sys.exit(0 if ok else 1)


async def _dispatch(args: argparse.Namespace) -> None:
    settings = get_settings()

    # Commands that don't need the database.
    if args.command == "seed" and args.action == "list":
        await _seed_list()
        return
    if args.command == "search":
        await _search(args.query, args.depth, args.max_tokens, args.as_json)
        return

    pool = await create_pool(settings.database_url)
    await migrate(pool)
    try:
        if args.command == "seed":
            if args.action == "status":
                await _seed_status(pool)
            elif args.action == "load":
                await _seed_load(pool, args.pack, args.directory)
        elif args.command == "keys":
            if args.action == "create":
                await _keys_create(pool, args.name)
            elif args.action == "list":
                await _keys_list(pool)
            elif args.action == "revoke":
                await _keys_revoke(pool, args.prefix)
        elif args.command == "stats":
            from redis.asyncio import Redis

            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            try:
                await _stats(pool, redis)
            finally:
                await redis.aclose()
        elif args.command == "doctor":
            from redis.asyncio import Redis

            redis = Redis.from_url(settings.redis_url, decode_responses=True)
            async with httpx.AsyncClient(follow_redirects=True) as http:
                try:
                    await _doctor(pool, redis, http)
                finally:
                    await redis.aclose()
    finally:
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(_dispatch(_parser().parse_args(argv)))


if __name__ == "__main__":
    main()
