"""`POST /v1/verify`: check a claim against sources.

Runs a search with the claim as the query, then classifies each passage as
supporting / contradicting / unrelated. With an LLM configured, one batched
call does the classification and the exact-quote constraint is checked in code;
without one, the verdict is always `unclear` and the top passages are returned
as supporting evidence ranked by relevance.
"""

from __future__ import annotations

import json
import logging

from sift.pipeline.candidates import domain_of
from sift.pipeline.rank import Ranked

log = logging.getLogger(__name__)

SYSTEM = (
    "You check whether passages support or contradict a claim. Reply with JSON only, in this "
    'shape: {"labels": [{"id": <int>, "label": "supporting" | "contradicting" | "unrelated", '
    '"confidence": <0..1>, "quote": "<exact substring from the passage>"}]}. The quote must be '
    "an exact substring of the passage."
)


def _verdict(supporting: list[dict], contradicting: list[dict]) -> tuple[str, float]:
    strong = [s for s in supporting if s["confidence"] >= 0.7]
    sites = {domain_of(s["url"]) for s in strong}
    contra_strong = [c for c in contradicting if c["confidence"] >= 0.8]
    contra_07 = [c for c in contradicting if c["confidence"] >= 0.7]

    if len(sites) >= 2 and not contra_07:
        return "supported", max(s["confidence"] for s in strong)
    if contra_strong and len(contra_strong) > len(strong):
        return "contradicted", max(c["confidence"] for c in contra_strong)
    all_conf = [s["confidence"] for s in supporting] + [c["confidence"] for c in contradicting]
    return "unclear", max(all_conf, default=0.0)


async def _classify(llm, claim: str, passages: list[Ranked]) -> list[dict]:
    numbered = [{"id": i, "text": r.passage.text} for i, r in enumerate(passages)]
    user = json.dumps({"claim": claim, "passages": numbered})
    result = await llm.chat_json(SYSTEM, user)
    labels = result.get("labels") if isinstance(result, dict) else None
    if not isinstance(labels, list):
        return []
    return [lab for lab in labels if isinstance(lab, dict)]


def _evidence(label: dict, passages: list[Ranked]) -> tuple[str, dict] | None:
    i = label.get("id")
    if not isinstance(i, int) or not (0 <= i < len(passages)):
        return None
    kind = label.get("label")
    if kind not in ("supporting", "contradicting"):
        return None
    quote = label.get("quote")
    if not isinstance(quote, str) or not quote or quote not in passages[i].passage.text:
        return None  # the quote must be an exact substring
    try:
        confidence = float(label.get("confidence", 0))
    except (TypeError, ValueError):
        return None
    p = passages[i].passage
    start = p.text.find(quote)
    evidence = {
        "url": p.page.url,
        "title": p.page.title,
        "quote": quote,
        "quote_start": p.start + start,
        "quote_end": p.start + start + len(quote),
        "confidence": min(max(confidence, 0.0), 1.0),
    }
    return kind, evidence


async def verify_claim(engine, claim: str, max_sources: int = 5) -> dict:
    from sift.pipeline.search import run_search

    outcome = await run_search(
        engine,
        claim,
        max_results=2 * max_sources,
        depth="advanced",
        include_raw_content=False,
    )
    passages = outcome.results

    llm = getattr(engine, "llm", None)
    if llm is None or not llm.configured:
        supporting = [
            {
                "url": r.passage.page.url,
                "title": r.passage.page.title,
                "quote": r.passage.text,
                "quote_start": r.passage.start,
                "quote_end": r.passage.end,
                "confidence": round(r.score / max(outcome.results[0].score, 1e-9), 4)
                if outcome.results
                else 0.0,
            }
            for r in passages[:max_sources]
        ]
        return {
            "verdict": "unclear",
            "confidence": 0.0,
            "supporting": supporting,
            "contradicting": [],
        }

    labels = await _classify(llm, claim, passages)
    supporting, contradicting = [], []
    for lab in labels:
        got = _evidence(lab, passages)
        if got is None:
            continue
        kind, evidence = got
        (supporting if kind == "supporting" else contradicting).append(evidence)

    verdict, confidence = _verdict(supporting, contradicting)
    return {
        "verdict": verdict,
        "confidence": round(confidence, 4),
        "supporting": supporting[:max_sources],
        "contradicting": contradicting[:max_sources],
    }
