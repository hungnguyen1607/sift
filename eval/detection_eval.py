"""Reproduce the vertical-detection numbers quoted in the README.

Rules first (``detect_vertical``); when the rules say plain ``web``, the
embedding kNN second opinion (``VerticalKNN``, the exact class the API uses)
may override. This measures two things, with no LLM and no API keys:

  1. Routing on ``HELD`` — phrasings written to be unlike the labelled
     examples in ``sift/pipeline/vertical_examples.py``, so they test
     generalization, not memorization.
  2. Misroutes on the 100 real web queries in ``eval/queries.jsonl`` — queries
     the rules already call ``web`` that the kNN must not drag elsewhere.

Run:  python eval/detection_eval.py
It loads the bge-small model (first run downloads it) and prints, e.g.:
  held-out 36/53 correct (rules alone: 31) · web queries misrouted 0/100
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sift.pipeline.embed import FastEmbedder
from sift.pipeline.vertical_knn import VerticalKNN
from sift.pipeline.verticals import detect_vertical

ROOT = Path(__file__).resolve().parent.parent

# Held-out phrasings the rules were NOT written for. Kept here, in the repo, so
# the README number is reproducible. Do not tune anything on this set.
HELD: dict[str, list[str]] = {
    "people": [
        "who are some designers working at figma",
        "linkedin of stripe engineers in dublin",
        "show me ML researchers from mila on linkedin",
        "twitter accounts of indie game devs",
        "people who work at openai",
    ],
    "jobs": [
        "is anyone hiring junior devs in montreal",
        "openings for nurses in calgary",
        "barista positions near me",
        "careers at shopify for backend devs",
        "part time work for students",
    ],
    "videos": [
        "clip of the moon landing",
        "show me a tutorial on knitting",
        "film trailers for dune",
        "music video for bohemian rhapsody",
        "lecture recordings on linear algebra",
    ],
    "shopping": [
        "where can i get airpods cheap",
        "deals on 4k monitors",
        "how much is a ps5 right now",
        "affordable ergonomic chairs",
        "gift ideas under 50 dollars",
    ],
    "places": [
        "sushi spots in kyoto",
        "where can i get a haircut in brooklyn",
        "late night food montreal plateau",
        "hospitals around ottawa",
        "parks in san francisco",
    ],
    "travel": [
        "planning a trip to peru",
        "what to see in prague in two days",
        "is bali worth visiting",
        "backpacking route through vietnam",
        "weekend getaway ideas from toronto",
    ],
    "news": [
        "what happened with the election results",
        "updates on the ukraine war",
        "anything new on the fed today",
        "breaking stories about apple",
        "today's tech headlines",
    ],
    "social": [
        "what does reddit think of the steam deck",
        "twitter reaction to the new iphone",
        "forum threads about sourdough starters",
        "discussions about vim vs emacs",
        "what are folks saying about the rust 2024 edition",
    ],
    "academic": [
        "literature review on remote work productivity",
        "meta-analysis of intermittent fasting",
        "scholarly articles on climate migration",
        "preprints about protein folding",
        "citations for transformer attention",
    ],
    "web": [
        "how do vaccines work",
        "what is the speed of light",
        "how to center a div",
        "who wrote pride and prejudice",
        "is it safe to eat raw eggs",
        "how to make cold brew coffee",
        "what does http 418 mean",
        "explain monads simply",
    ],
}


async def main() -> None:
    embedder = FastEmbedder()
    knn = VerticalKNN(embedder)
    knn.warm()
    if knn._task is not None:
        await knn._task  # build the example vectors before routing
    assert knn.ready, "kNN did not warm up"

    async def route(query: str) -> str:
        return await knn.route(query, detect_vertical(query))

    held = [(v, q) for v, qs in HELD.items() for q in qs]
    rules_ok = sum(detect_vertical(q) == v for v, q in held)
    final_ok = 0
    for v, q in held:
        if await route(q) == v:
            final_ok += 1

    lines = (ROOT / "eval/queries.jsonl").read_text().splitlines()
    web_queries = [json.loads(line)["query"] for line in lines if line.strip()]
    rule_web = [q for q in web_queries if detect_vertical(q) == "web"]
    misrouted = [q for q in rule_web if await route(q) != "web"]

    print(f"held-out {final_ok}/{len(held)} correct (rules alone: {rules_ok})")
    print(f"web queries misrouted {len(misrouted)}/{len(rule_web)}")
    if misrouted:
        print("  misrouted:", misrouted)


if __name__ == "__main__":
    asyncio.run(main())
