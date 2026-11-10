"""LLM judge: label each top-5 result 0 (irrelevant) / 1 (partial) / 2 (answers).

Blind: results from all systems are shuffled together so the judge can't see
which system produced them. One batched call per query. Writes
``eval/runs/<date>/judged.jsonl`` (one line per query per system).

    python eval/judge.py eval/runs/2026-11-09

Env: ``SIFT_LLM_PROVIDER`` / ``SIFT_LLM_BASE_URL`` / ``SIFT_LLM_API_KEY`` /
``SIFT_LLM_MODEL`` (or pass --provider/--model/--api-key). Also reads
``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY`` as fallbacks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
from pathlib import Path

from sift.pipeline.llm import LLM

SYSTEM_PROMPT = (
    "You are grading search results for relevance. For each result, score how well it answers "
    "the query: 0 = irrelevant, 1 = partially relevant, 2 = answers the query. Reply with JSON "
    'only: {"labels": [{"id": <int>, "label": 0|1|2}, ...]} for every result id given.'
)


def _llm(args: argparse.Namespace) -> LLM:
    provider = args.provider or os.environ.get("SIFT_LLM_PROVIDER") or "anthropic"
    model = args.model or os.environ.get("SIFT_LLM_MODEL")
    api_key = (
        args.api_key
        or os.environ.get("SIFT_LLM_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
    )
    return LLM(provider, os.environ.get("SIFT_LLM_BASE_URL"), api_key, model)


async def label_query(
    llm: LLM, query: str, results_by_system: dict[str, list[dict]]
) -> dict[str, list[int]]:
    combined = []
    for system, results in results_by_system.items():
        for i, r in enumerate(results):
            combined.append(
                {"tag": (system, i), "title": r.get("title"), "content": r.get("content")}
            )
    random.shuffle(combined)

    items = [
        {"id": i, "title": c["title"], "content": (c["content"] or "")[:800]}
        for i, c in enumerate(combined)
    ]
    result = await llm.chat_json(SYSTEM_PROMPT, json.dumps({"query": query, "results": items}))
    labels_by_id: dict[int, int] = {}
    for lab in (result or {}).get("labels", []):
        if isinstance(lab, dict) and isinstance(lab.get("id"), int):
            labels_by_id[lab["id"]] = int(lab.get("label", 0))

    out: dict[str, list[int]] = {system: [0] * 5 for system in results_by_system}
    for i, c in enumerate(combined):
        system, idx = c["tag"]
        out[system][idx] = labels_by_id.get(i, 0)
    return out


def _run_files(run_dir: Path) -> list[Path]:
    return sorted(p for p in run_dir.glob("*.jsonl") if p.name != "judged.jsonl")


async def main(run_dir: Path, args: argparse.Namespace) -> None:
    llm = _llm(args)
    if not llm.configured:
        raise SystemExit("no judge model configured (set SIFT_LLM_MODEL or --model)")

    systems: dict[str, list[dict]] = {}
    for path in await asyncio.to_thread(_run_files, run_dir):
        systems[path.stem] = [
            json.loads(line) for line in path.read_text().splitlines() if line.strip()
        ]

    out_path = run_dir / "judged.jsonl"
    with out_path.open("w") as f:
        for i in range(len(next(iter(systems.values())))):
            query = systems[next(iter(systems))][i]["query"]
            qid = systems[next(iter(systems))][i]["query_id"]
            category = systems[next(iter(systems))][i].get("category")
            results_by_system = {
                system: records[i].get("results", []) for system, records in systems.items()
            }
            labels = await label_query(llm, query, results_by_system)
            for system, lab in labels.items():
                f.write(
                    json.dumps(
                        {"query_id": qid, "category": category, "system": system, "labels": lab}
                    )
                    + "\n"
                )
    print(f"judged → {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    ap.add_argument("--provider")
    ap.add_argument("--model")
    ap.add_argument("--api-key")
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args()
    random.seed(a.seed)
    import asyncio

    asyncio.run(main(a.run_dir, a))
