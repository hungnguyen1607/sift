"""Print the Sift-vs-Tavily relevance tables as Markdown, from judged labels.

python eval/compare_tavily.py eval/runs/2026-11-09/judged.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .metrics import mean, ndcg_at_k, relevance_at_k  # package import
except ImportError:
    from metrics import mean, ndcg_at_k, relevance_at_k  # script run

SYSTEMS = ["sift_basic", "sift_advanced", "tavily"]


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _cell(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def print_overall(judged: list[dict]) -> None:
    systems = sorted({j["system"] for j in judged})
    print("## Relevance (all 100 queries)")
    print()
    print("| system | relevance@5 | nDCG@5 |")
    print("|---|---|---|")
    by_system: dict[str, list[list[int]]] = {s: [] for s in systems}
    for j in judged:
        by_system[j["system"]].append(j["labels"])
    for system in systems:
        labels = by_system[system]
        rel = mean([relevance_at_k(x) for x in labels])
        ndcg = mean([ndcg_at_k(x) for x in labels])
        print(f"| {system} | {_cell(rel)} | {_cell(ndcg)} |")


def print_by_category(judged: list[dict]) -> None:
    systems = sorted({j["system"] for j in judged})
    categories = sorted({j.get("category", "?") for j in judged})
    print()
    print("## Relevance@5 by category")
    print()
    header = "| category | " + " | ".join(systems) + " |"
    print(header)
    print("|" + "---|" * (len(systems) + 1))
    for cat in categories:
        cells = []
        for system in systems:
            labels = [
                j["labels"] for j in judged if j["system"] == system and j.get("category") == cat
            ]
            cells.append(_cell(mean([relevance_at_k(x) for x in labels])))
        print(f"| {cat} | " + " | ".join(cells) + " |")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("judged", type=Path)
    args = ap.parse_args()
    judged = load(args.judged)
    print_overall(judged)
    print_by_category(judged)


if __name__ == "__main__":
    main()
