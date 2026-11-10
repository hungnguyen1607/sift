"""Index maintenance: keep the local index bounded by evicting pages that
haven't been used recently (LRU). Runs in the worker; passages cascade."""

from __future__ import annotations

import asyncpg


async def evict_pages(conn: asyncpg.Connection, max_pages: int = 0, retention_days: int = 0) -> int:
    """Delete pages unused for ``retention_days`` and then any beyond
    ``max_pages`` (least-recently-used first). Returns pages deleted.
    ``0`` disables that rule."""
    deleted = 0
    if retention_days > 0:
        deleted += await conn.fetchval(
            """
            WITH gone AS (
                DELETE FROM pages
                WHERE last_used_at < now() - make_interval(days => $1)
                RETURNING id
            )
            SELECT count(*) FROM gone
            """,
            retention_days,
        )
    if max_pages > 0:
        deleted += await conn.fetchval(
            """
            WITH gone AS (
                DELETE FROM pages WHERE id IN (
                    SELECT id FROM pages ORDER BY last_used_at DESC, id DESC OFFSET $1
                )
                RETURNING id
            )
            SELECT count(*) FROM gone
            """,
            max_pages,
        )
    return deleted
