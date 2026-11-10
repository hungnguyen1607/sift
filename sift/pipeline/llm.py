"""A tiny LLM client for the optional answer, planner and verify features.

Talks to an Anthropic Messages endpoint or any OpenAI-compatible
`/chat/completions` endpoint (Ollama, vLLM, LM Studio, …). Off unless
``SIFT_LLM_*`` is configured; every call is a plain request with a timeout.
"""

from __future__ import annotations

import json
import logging

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_DEFAULT = "https://api.anthropic.com"
OPENAI_DEFAULT = "https://api.openai.com/v1"


class LLM:
    def __init__(
        self,
        provider: str | None,
        base_url: str | None,
        api_key: str | None,
        model: str | None,
        timeout: float = 4.0,
    ):
        self.provider = (provider or "").lower()
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        if self.provider == "anthropic":
            self.base_url = (base_url or ANTHROPIC_DEFAULT).rstrip("/")
        else:
            self.base_url = (base_url or OPENAI_DEFAULT).rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.model)

    async def chat(self, system: str, user: str, timeout_s: float | None = None) -> str | None:
        timeout_s = timeout_s or self.timeout
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                if self.provider == "anthropic":
                    return await self._anthropic(client, system, user)
                return await self._openai(client, system, user)
        except Exception as e:  # network, HTTP, parse — answer/verify degrade to null
            log.warning("LLM call failed: %r", e)
            return None

    async def _anthropic(self, client: httpx.AsyncClient, system: str, user: str) -> str | None:
        resp = await client.post(
            f"{self.base_url}/v1/messages",
            headers={"x-api-key": self.api_key or "", "anthropic-version": "2023-06-01"},
            json={
                "model": self.model,
                "max_tokens": 1024,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return "".join(
            b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"
        )

    async def _openai(self, client: httpx.AsyncClient, system: str, user: str) -> str | None:
        resp = await client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key or ''}"},
            json={
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"]["content"]

    async def chat_json(self, system: str, user: str) -> dict | None:
        text = await self.chat(system, user)
        if not text:
            return None
        # Tolerate a leading ```json fence and surrounding prose.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            return None
