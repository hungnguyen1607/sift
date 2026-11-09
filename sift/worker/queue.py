"""A Postgres job queue. Workers claim jobs with FOR UPDATE SKIP LOCKED, so any
number of them can run without taking the same job twice."""

from __future__ import annotations

import json
from dataclasses import dataclass

import asyncpg

MAX_ATTEMPTS = 3


@dataclass(slots=True)
class Job:
    id: int
    kind: str
    payload: dict
    attempts: int


async def enqueue(pool: asyncpg.Pool, kind: str, payload: dict) -> int:
    return await pool.fetchval(
        "INSERT INTO jobs (kind, payload) VALUES ($1, $2::jsonb) RETURNING id",
        kind,
        json.dumps(payload, default=str),
    )


async def claim(conn: asyncpg.Connection) -> Job | None:
    row = await conn.fetchrow(
        """
        UPDATE jobs SET status = 'running', attempts = attempts + 1, updated_at = now()
        WHERE id = (
            SELECT id FROM jobs
            WHERE status = 'queued' AND run_after <= now()
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING id, kind, payload, attempts
        """
    )
    if row is None:
        return None
    return Job(row["id"], row["kind"], json.loads(row["payload"]), row["attempts"])


async def complete(conn: asyncpg.Connection, job: Job) -> None:
    await conn.execute(
        "UPDATE jobs SET status = 'done', last_error = NULL, updated_at = now() WHERE id = $1",
        job.id,
    )


async def fail(conn: asyncpg.Connection, job: Job, error: str) -> None:
    """Retry with backoff (30 s, 2 min) until MAX_ATTEMPTS, then mark failed."""
    final = job.attempts >= MAX_ATTEMPTS
    await conn.execute(
        """
        UPDATE jobs SET status = $2, last_error = $3, updated_at = now(),
               run_after = now() + make_interval(secs => $4)
        WHERE id = $1
        """,
        job.id,
        "failed" if final else "queued",
        error[:2000],
        30.0 * 4 ** (job.attempts - 1),
    )


async def maintain(conn: asyncpg.Connection) -> None:
    """Requeue jobs stuck in 'running' (worker died) and drop old finished ones."""
    await conn.execute(
        "UPDATE jobs SET status = 'queued', updated_at = now() "
        "WHERE status = 'running' AND updated_at < now() - interval '10 minutes'"
    )
    await conn.execute(
        "DELETE FROM jobs WHERE status = 'done' AND updated_at < now() - interval '1 day'"
    )
