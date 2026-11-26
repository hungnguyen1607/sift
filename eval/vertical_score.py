"""Score Sift's vertical searches with rules (no LLM, no API keys) and write a
scorecard.

    SIFT_URL=http://localhost:8077 python eval/vertical_score.py
    python eval/vertical_score.py --only people,jobs --gap 6
    python eval/vertical_score.py --rescore eval/runs/2026-11-17/verticals.jsonl

Each query in ``eval/verticals.jsonl`` names the vertical it should be routed to
and the checks a good answer passes. Every check scores 0..1 and a query's score
is their mean:

- ``detected``: the response's ``vertical`` is the expected one
- ``count``: results / ``min_results`` (capped at 1)
- ``must``: share of the top 5 whose title/content/fields contain every term
- ``url_re``: share of results whose URL matches (the right kind of page)
- ``fields_any``: share of results with at least one of these structured fields
- ``near``: share of places within ``km`` of [lat, lon]

``force: true`` sends the vertical explicitly instead of relying on detection.
Raw responses are saved next to the scorecard, so a rescore costs nothing.
Queries run one at a time with a gap between them: the engines behind SearXNG
rate-limit bursts, and a scorecard measured while they do is measuring that.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import time
from datetime import date
from pathlib import Path

import httpx

HERE = Path(__file__).parent
QUERIES = HERE / "verticals.jsonl"


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _hay(r: dict) -> str:
    return (
        f"{r.get('title', '')} {r.get('content', '')} {json.dumps(r.get('fields') or {})}".lower()
    )


def _km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = (
        math.sin((la2 - la1) / 2) ** 2
        + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    )
    return 12742 * math.asin(math.sqrt(h))


def score(q: dict, resp: dict | None) -> dict[str, float]:
    """Per-check scores (0..1) for one query's response."""
    if resp is None:
        return {"detected": 0.0, "count": 0.0}
    res = resp.get("results") or []
    checks: dict[str, float] = {}
    if not q.get("force"):
        checks["detected"] = float(resp.get("vertical") == q["vertical"])
    checks["count"] = min(1.0, len(res) / q.get("min_results", 3))
    if res and q.get("must"):
        top = res[:5]
        checks["must"] = sum(all(t in _hay(r) for t in q["must"]) for r in top) / len(top)
    if q.get("url_re"):
        rx = re.compile(q["url_re"])
        checks["url_re"] = sum(bool(rx.search(r["url"])) for r in res) / len(res) if res else 0.0
    if q.get("fields_any"):
        want = set(q["fields_any"])
        checks["fields_any"] = (
            sum(bool(want & set(r.get("fields") or {})) for r in res) / len(res) if res else 0.0
        )
    if q.get("near"):
        lat, lon, km = q["near"]
        placed = [r for r in res if (r.get("fields") or {}).get("lat") is not None]
        checks["near"] = (
            sum(_km((lat, lon), (r["fields"]["lat"], r["fields"]["lon"])) <= km for r in placed)
            / len(placed)
            if placed
            else 0.0
        )
    return checks


def run(queries: list[dict], base: str, gap: float, out: Path) -> list[dict]:
    headers = (
        {"Authorization": f"Bearer {os.environ['SIFT_API_KEY']}"}
        if os.environ.get("SIFT_API_KEY")
        else {}
    )
    rows = []
    with httpx.Client(timeout=60) as client, out.open("w") as f:
        for i, q in enumerate(queries):
            if i:
                time.sleep(gap)
            body = {"query": q["query"], "max_results": max(10, q.get("min_results", 3))}
            if q.get("force"):
                body["vertical"] = q["vertical"]
            t = time.perf_counter()
            try:
                r = client.post(f"{base}/v1/search", json=body, headers=headers)
                r.raise_for_status()
                resp, err = r.json(), None
            except Exception as e:  # recorded, scored as a miss
                resp, err = None, str(e)[:200]
            row = {
                **q,
                "elapsed": round(time.perf_counter() - t, 3),
                "error": err,
                "response": resp,
            }
            f.write(json.dumps(row) + "\n")
            f.flush()
            rows.append(row)
            s = score(q, resp)
            print(f"{q['id']:12} {statistics.mean(s.values()):.2f} {row['elapsed']:5.1f}s {s}")
    return rows


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))]


def scorecard(rows: list[dict]) -> str:
    by_v: dict[str, list[dict]] = {}
    for r in rows:
        by_v.setdefault(r["vertical"], []).append(r)
    lines = [
        f"# Sift vertical scorecard ({date.today().isoformat()})",
        "",
        "Rule-based checks (see `eval/vertical_score.py`); 1.00 is perfect.",
        "Latency is wall time per request, cache bypassed (cache hits excluded).",
        "",
        "| vertical | queries | score | detected | results ok | on-topic | right pages "
        "| fields | stale/empty | p50 s | p95 s |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    all_scores, all_t = [], []
    for v, rs in by_v.items():
        per = [score(r, r["response"]) for r in rs]
        qs = [statistics.mean(s.values()) for s in per]
        all_scores += qs

        def col(name: str, per=per) -> str:
            xs = [s[name] for s in per if name in s]
            return f"{statistics.mean(xs):.2f}" if xs else "—"

        ts = [r["elapsed"] for r in rs if r["response"] and not r["response"].get("cache_hit")]
        all_t += ts
        bad = sum(
            1
            for r in rs
            if not r["response"] or not r["response"]["results"] or r["response"].get("stale")
        )
        fields = col("fields_any") + (f" / near {col('near')}" if v == "places" else "")
        cells = [
            v,
            str(len(rs)),
            f"**{statistics.mean(qs):.2f}**",
            col("detected"),
            col("count"),
            col("must"),
            col("url_re"),
            fields,
            str(bad),
            f"{_pct(ts, 0.5):.1f}",
            f"{_pct(ts, 0.95):.1f}",
        ]
        lines.append("| " + " | ".join(cells) + " |")
    lines += [
        "",
        f"**Overall: {statistics.mean(all_scores):.2f}** over {len(rows)} queries; "
        f"p50 {_pct(all_t, 0.5):.1f} s, p95 {_pct(all_t, 0.95):.1f} s.",
        "",
        "## Weakest queries",
        "",
    ]
    worst = sorted(rows, key=lambda r: statistics.mean(score(r, r["response"]).values()))[:10]
    for r in worst:
        s = score(r, r["response"])
        miss = ", ".join(f"{k} {v:.2f}" for k, v in s.items() if v < 1)
        skipped = (r["response"] or {}).get("providers_skipped") or {}
        note = r["error"] or "; ".join(
            f"{k}: {v[:60]}"
            for k, v in skipped.items()
            if k not in ("openalex", "arxiv", "marginalia")
        )
        lines.append(
            f"- `{r['id']}` {r['query']!r}: {statistics.mean(s.values()):.2f} ({miss}) {note}"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated verticals")
    ap.add_argument("--gap", type=float, default=4.0, help="seconds between queries")
    ap.add_argument("--rescore", type=Path, help="score a saved verticals.jsonl run")
    ap.add_argument("--out", type=Path, default=HERE / "runs" / date.today().isoformat())
    args = ap.parse_args()
    if args.rescore:
        rows = load(args.rescore)
        out_dir = args.rescore.parent
    else:
        queries = load(QUERIES)
        if args.only:
            keep = set(args.only.split(","))
            queries = [q for q in queries if q["vertical"] in keep]
        args.out.mkdir(parents=True, exist_ok=True)
        base = os.environ.get("SIFT_URL", "http://localhost:8000").rstrip("/")
        rows = run(queries, base, args.gap, args.out / "verticals.jsonl")
        out_dir = args.out
    card = scorecard(rows)
    (out_dir / "SCORECARD.md").write_text(card)
    print("\n" + card)


if __name__ == "__main__":
    main()
