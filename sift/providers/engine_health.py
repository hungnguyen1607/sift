"""Cooldowns for the engines behind SearXNG.

Every SearXNG response lists its ``unresponsive_engines`` with a reason. An
engine that answered "too many requests" or showed a CAPTCHA keeps failing
for minutes, and each new request to it only extends the block (DuckDuckGo
CAPTCHAs never lift while requests keep coming). So Sift remembers those
engines and asks SearXNG to skip them (its per-request ``disabled_engines``
preference) until the cooldown ends. Repeat offenders cool down longer, up to
``max_cooldown_s``. Timeouts and parsing errors are per-query noise, not
blocks, and are ignored.

The state is per process, which matches the API's single uvicorn process.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

# Reasons (lowercased substrings) that mean "blocked for a while".
_BLOCK_REASONS = ("too many requests", "captcha", "access denied", "suspended", "forbidden")


@dataclass(slots=True)
class _State:
    until: float = 0.0
    strikes: int = 0
    reason: str = ""
    last_seen: float = 0.0


class EngineHealth:
    def __init__(
        self,
        cooldown_s: float = 180.0,
        max_cooldown_s: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.cooldown_s = cooldown_s
        self.max_cooldown_s = max_cooldown_s
        self.clock = clock
        self._engines: dict[str, _State] = {}

    @staticmethod
    def is_block(reason: str) -> bool:
        r = reason.lower()
        return any(b in r for b in _BLOCK_REASONS)

    def report(self, unresponsive: Iterable) -> None:
        """Record a response's ``unresponsive_engines`` ([name, reason] pairs)."""
        now = self.clock()
        for item in unresponsive or ():
            try:
                name, reason = str(item[0]), str(item[1])
            except (IndexError, TypeError):
                continue
            if not self.is_block(reason):
                continue
            st = self._engines.setdefault(name, _State())
            if st.until > now:
                continue  # already cooling (SearXNG still lists it as suspended)
            # Blocked again soon after the last cooldown ended: the block didn't lift.
            again = st.strikes > 0 and now - st.until < self.cooldown_s
            st.strikes = st.strikes + 1 if again else 1
            span = min(self.cooldown_s * 2 ** (st.strikes - 1), self.max_cooldown_s)
            st.until = now + span
            st.reason = reason
            st.last_seen = now

    def cooling(self) -> list[str]:
        now = self.clock()
        return sorted(n for n, st in self._engines.items() if st.until > now)

    def disabled_param(self, categories: str) -> str | None:
        """The ``disabled_engines`` value for a request in ``categories``
        (SearXNG keys engines as ``name__category``)."""
        names = self.cooling()
        if not names:
            return None
        cats = [c.strip() for c in categories.split(",") if c.strip()] or ["general"]
        return ",".join(f"{n}__{c}" for n in names for c in cats)

    def snapshot(self) -> dict[str, dict]:
        now = self.clock()
        return {
            n: {
                "cooling": st.until > now,
                "retry_in_s": max(0, round(st.until - now)),
                "strikes": st.strikes,
                "reason": st.reason,
            }
            for n, st in sorted(self._engines.items())
        }
