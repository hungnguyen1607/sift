"""Token efficiency: tokens returned per useful answer, Sift vs Tavily.

The headline agent metric. For each query a system "answers" when any top-5
result is judged 2. Sift's cost is its reported ``tokens_used`` (packed to
``max_tokens``); Tavily reports no token count, so its content is counted with
the same tokenizer.

    python eval/token_efficiency.py eval/runs/2026-11-09
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .metrics import token_efficiency  # package import
except ImportError:
    from metrics import token_efficiency  # script run

from sift.pipeline.pack import count_tokens


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def tokens_for(record: dict) -> int:
    if record.get("tokens_used") is not None:
        return int(record["tokens_used"])
    return sum(count_tokens(r.get("content") or "") for r in record.get("results", []))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir", type=Path)
    args = ap.parse_args()

    run_dir: Path = args.run_dir
    judged = load(run_dir / "judged.jsonl")
    answered: dict[tuple[str, str], bool] = {
        (j["query_id"], j["system"]): (2 in j["labels"]) for j in judged
    }

    systems = sorted({j["system"] for j in judged})
    print("## Token efficiency (tokens per useful answer)")
    print()
    print("| system | tokens/answer | queries answered |")
    print("|---|---|---|")
    for system in systems:
        records = {}
        for path in run_dir.glob("*.jsonl"):
            if path.name == "judged.jsonl":
                continue
            for rec in load(path):
                if rec.get("system") == system and "query_id" in rec:
                    records[rec["query_id"]] = rec
        pairs = [
            (float(tokens_for(rec)), answered.get((qid, system), False))
            for qid, rec in records.items()
        ]
        eff = token_efficiency(pairs)
        n_answered = sum(1 for _, a in pairs if a)
        eff_s = "—" if eff is None else f"{eff:.0f}"
        print(f"| {system} | {eff_s} | {n_answered} |")


if __name__ == "__main__":
    main()
