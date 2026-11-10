"""API-key authentication. ``SIFT_AUTH=off`` (default) allows anonymous use for
local self-hosting; ``on`` requires a Bearer token or a Tavily-style ``api_key``
in the JSON body. Keys are stored as sha256 only."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from typing import Annotated

import asyncpg
from fastapi import Depends, HTTPException, Request

from sift.config import Settings, get_settings

KEY_PREFIX = "sk_sift_"


@dataclass(slots=True)
class ApiKey:
    id: int
    name: str
    prefix: str
    rpm_limit: int
    monthly_limit: int | None


def new_key() -> tuple[str, str, str]:
    """Return (full key, prefix, sha256 hash)."""
    raw = KEY_PREFIX + secrets.token_urlsafe(32)
    return raw, raw[: len(KEY_PREFIX) + 12], hash_key(raw)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyStore:
    """Looks keys up in Postgres, with a short in-process cache."""

    def __init__(self, pool: asyncpg.Pool, ttl: float = 60.0):
        self.pool = pool
        self.ttl = ttl
        self._cache: dict[str, tuple[float, ApiKey | None]] = {}

    async def get(self, key_hash: str) -> ApiKey | None:
        import time

        hit = self._cache.get(key_hash)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        row = await self.pool.fetchrow(
            "SELECT id, name, prefix, rpm_limit, monthly_limit FROM api_keys "
            "WHERE key_hash = $1 AND revoked_at IS NULL",
            key_hash,
        )
        key = (
            ApiKey(row["id"], row["name"], row["prefix"], row["rpm_limit"], row["monthly_limit"])
            if row
            else None
        )
        if len(self._cache) > 10_000:
            self._cache.clear()
        self._cache[key_hash] = (time.monotonic() + self.ttl, key)
        return key

    async def is_revoked(self, key_hash: str) -> bool:
        row = await self.pool.fetchval(
            "SELECT 1 FROM api_keys WHERE key_hash = $1 AND revoked_at IS NOT NULL", key_hash
        )
        return row is not None


def _extract_key(request: Request, body: dict) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    api_key = body.get("api_key")
    return api_key if isinstance(api_key, str) and api_key else None


class KeyDep:
    def __init__(self):
        self._cache: dict[str, tuple[float, ApiKey | None]] = {}

    async def __call__(self, request: Request) -> ApiKey | None:
        settings: Settings = get_settings()
        if settings.auth != "on":
            return None
        body = await _body(request)
        token = _extract_key(request, body)
        store: KeyStore = request.app.state.key_store
        if token is None or not token.startswith(KEY_PREFIX):
            raise HTTPException(status_code=401, detail="missing or invalid API key")
        digest = hash_key(token)
        key = await store.get(digest)
        if key is None:
            if await store.is_revoked(digest):
                raise HTTPException(status_code=403, detail="revoked API key")
            raise HTTPException(status_code=401, detail="invalid API key")
        return key


async def _body(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


key_dep = KeyDep()
ApiKeyDep = Annotated[ApiKey | None, Depends(key_dep)]
