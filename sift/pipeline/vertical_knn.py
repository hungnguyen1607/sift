"""A second opinion for queries the rules leave as plain web search.

The regex rules in ``verticals.py`` route the phrasings they were written for,
but miss new ones ("is anyone hiring junior devs in montreal", "lecture
recordings on linear algebra"). This compares the query's embedding (the same
bge-small model Sift already uses for ranking, so no new model) with the
labelled phrasings in ``vertical_examples.py`` and takes a weighted vote of
the ``k`` nearest.

It only overrides "web", and only when the nearest example is close (cosine
≥ ``min_top``) and the winning vertical out-votes "web" by ``margin``. Looser
settings rescue more queries but misroute ordinary questions ("When was Python
first released?" looks like a code search): measured on 53 unseen vertical
phrasings and the 100 real web queries in ``eval/queries.jsonl``, the defaults
route 36/53 (rules alone: 31) and misroute 0/100 web queries.

The example vectors are embedded in the background on first use; until they
are ready, the rules decide alone, so no request waits for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter

from sift.pipeline.embed import Embedder, cosine
from sift.pipeline.vertical_examples import EXAMPLES

log = logging.getLogger(__name__)


class VerticalKNN:
    def __init__(
        self,
        embedder: Embedder,
        examples: dict[str, list[str]] = EXAMPLES,
        *,
        k: int = 7,
        margin: float = 0.1,
        min_top: float = 0.8,
    ):
        self.embedder = embedder
        self.examples = [(v, q) for v, qs in examples.items() for q in qs]
        self.k, self.margin, self.min_top = k, margin, min_top
        self._vecs: list[list[float]] | None = None
        self._task: asyncio.Task | None = None

    @property
    def ready(self) -> bool:
        return self._vecs is not None

    def warm(self) -> None:
        """Start embedding the examples, if not started yet."""
        if self._task is None:
            self._task = asyncio.ensure_future(self._build())

    async def _build(self) -> None:
        try:
            vecs = []
            for _, q in self.examples:  # embed_query: the same space as live queries
                vecs.append(await self.embedder.embed_query(q))
            self._vecs = vecs
            log.info("vertical knn ready: %d examples", len(vecs))
        except Exception as e:  # stay rules-only
            log.warning("vertical knn unavailable: %r", e)

    def vote(self, qvec: list[float]) -> tuple[str, float, float]:
        """(winning vertical, its lead over "web" per neighbour, top cosine)."""
        assert self._vecs is not None
        sims = sorted(
            ((cosine(qvec, w), self.examples[i][0]) for i, w in enumerate(self._vecs)),
            reverse=True,
        )[: self.k]
        votes: Counter[str] = Counter()
        for s, v in sims:
            votes[v] += s
        best, score = votes.most_common(1)[0]
        return best, (score - votes.get("web", 0.0)) / self.k, sims[0][0]

    async def route(self, query: str, rule_choice: str) -> str:
        if rule_choice != "web":
            return rule_choice
        if not self.ready:
            self.warm()
            return rule_choice
        try:
            qvec = await asyncio.wait_for(self.embedder.embed_query(query), 0.3)
        except Exception:
            return rule_choice
        best, lead, top = self.vote(qvec)
        if best != "web" and lead >= self.margin and top >= self.min_top:
            log.info("vertical knn: %r → %s (lead %.2f, top %.2f)", query, best, lead, top)
            return best
        return rule_choice
