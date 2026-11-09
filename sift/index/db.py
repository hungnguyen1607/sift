"""Connection pool and a minimal forward-only SQL migrator."""

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)
MIGRATIONS = Path(__file__).parent / "migrations"
_LOCK_ID = 7_314_202_601  # pg_advisory_lock key, so concurrent replicas migrate once


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=10)


async def migrate(pool: asyncpg.Pool) -> list[str]:
    applied_now: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_ID)
        try:
            await conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations "
                "(name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(MIGRATIONS.glob("*.sql")):
                if path.name in done:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute("INSERT INTO schema_migrations(name) VALUES ($1)", path.name)
                log.info("applied migration %s", path.name)
                applied_now.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_ID)
    return applied_now
