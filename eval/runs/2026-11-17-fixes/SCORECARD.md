# Sift vertical scorecard (2026-11-17)

Rule-based checks (see `eval/vertical_score.py`); 1.00 is perfect.
Latency is wall time per request, cache bypassed (cache hits excluded).

| vertical | queries | score | detected | results ok | on-topic | right pages | fields | stale/empty | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|
| news | 5 | **1.00** | 1.00 | 1.00 | 1.00 | — | 1.00 | 0 | 1.1 | 2.9 |
| places | 5 | **0.97** | 1.00 | 0.87 | — | — | 1.00 / near 1.00 | 0 | 0.9 | 3.0 |
| site | 5 | **0.97** | 1.00 | 1.00 | 0.90 | 1.00 | — | 0 | 1.3 | 2.0 |

**Overall: 0.98** over 15 queries; p50 1.1 s, p95 2.9 s.

## Weakest queries

- `places-04` 'gyms near downtown toronto': 0.83 (count 0.33) 
- `site-04` 'site:docs.python.org dataclasses': 0.88 (must 0.50) 
- `news-01` 'latest openai news': 1.00 () 
- `news-02` "what's new with spacex": 1.00 () 
- `news-03` 'nvidia news this week': 1.00 () 
- `news-04` 'latest news on the bank of canada interest rate': 1.00 () 
- `news-05` 'recent headlines about electric vehicles': 1.00 () 
- `places-01` 'cafes near Concordia University': 1.00 () 
- `places-02` 'thai restaurants in vancouver': 1.00 () 
- `places-03` 'museums in paris': 1.00 () 
