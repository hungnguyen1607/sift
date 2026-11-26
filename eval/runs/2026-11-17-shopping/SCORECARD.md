# Sift vertical scorecard (2026-11-17)

Rule-based checks (see `eval/vertical_score.py`); 1.00 is perfect.
Latency is wall time per request, cache bypassed (cache hits excluded).

| vertical | queries | score | detected | results ok | on-topic | right pages | fields | stale/empty | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|
| shopping | 5 | **0.77** | 1.00 | 1.00 | 1.00 | — | 0.06 | 0 | 2.9 | 3.5 |

**Overall: 0.77** over 5 queries; p50 2.9 s, p95 3.5 s.

## Weakest queries

- `shopping-03` 'cheapest noise cancelling headphones': 0.75 (fields_any 0.00) 
- `shopping-04` 'best price for a standing desk': 0.75 (fields_any 0.00) 
- `shopping-05` 'buy running shoes for women': 0.75 (fields_any 0.00) 
- `shopping-02` 'buy keychron k2 keyboard': 0.78 (fields_any 0.10) 
- `shopping-01` 'mechanical keyboard under $100': 0.80 (fields_any 0.20) 
