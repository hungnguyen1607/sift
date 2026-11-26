# Sift vertical scorecard (2026-11-17)

Rule-based checks (see `eval/vertical_score.py`); 1.00 is perfect.
Latency is wall time per request, cache bypassed (cache hits excluded).

| vertical | queries | score | detected | results ok | on-topic | right pages | fields | stale/empty | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|
| web | 5 | **0.88** | 1.00 | 1.00 | 0.64 | — | — | 0 | 2.3 | 2.7 |
| news | 5 | **0.96** | 1.00 | 1.00 | 1.00 | — | 0.83 | 0 | 1.5 | 2.0 |
| videos | 5 | **0.93** | 1.00 | 1.00 | 1.00 | 0.64 | 1.00 | 0 | 1.5 | 3.1 |
| people | 5 | **0.93** | 1.00 | 1.00 | 0.95 | 1.00 | 0.71 | 0 | 1.2 | 1.6 |
| social | 5 | **0.96** | 1.00 | 1.00 | 1.00 | 0.67 | 1.00 | 0 | 1.6 | 2.4 |
| jobs | 5 | **0.74** | 0.80 | 1.00 | 0.96 | — | 0.18 | 0 | 3.1 | 3.6 |
| shopping | 5 | **0.75** | 1.00 | 1.00 | 1.00 | — | 0.00 | 0 | 2.7 | 3.0 |
| places | 5 | **0.97** | 1.00 | 0.87 | — | — | 1.00 / near 1.00 | 0 | 2.9 | 3.0 |
| travel | 5 | **1.00** | 1.00 | 1.00 | 1.00 | — | — | 0 | 2.3 | 2.6 |
| academic | 5 | **0.88** | 1.00 | 1.00 | 0.64 | — | — | 0 | 1.7 | 4.5 |
| code | 5 | **0.88** | — | 1.00 | 0.76 | — | — | 0 | 2.2 | 3.2 |
| site | 5 | **0.85** | 1.00 | 0.80 | 0.90 | 0.80 | — | 1 | 1.0 | 2.0 |

**Overall: 0.89** over 60 queries; p50 2.0 s, p95 3.2 s.

## Weakest queries

- `site-05` 'site:developer.mozilla.org fetch api': 0.33 (count 0.00, url_re 0.00) searxng: searxng: ReadTimeout: 
- `jobs-05` 'hiring frontend react developer': 0.50 (detected 0.00, fields_any 0.00) 
- `code-02` 'python typeerror nonetype object is not subscriptable': 0.70 (must 0.40) 
- `web-03` 'why is the sky blue': 0.73 (must 0.20) 
- `jobs-03` 'summer internships in software engineering': 0.75 (fields_any 0.00) 
- `shopping-01` 'mechanical keyboard under $100': 0.75 (fields_any 0.00) 
- `shopping-02` 'buy keychron k2 keyboard': 0.75 (fields_any 0.00) 
- `shopping-03` 'cheapest noise cancelling headphones': 0.75 (fields_any 0.00) 
- `shopping-04` 'best price for a standing desk': 0.75 (fields_any 0.00) 
- `shopping-05` 'buy running shoes for women': 0.75 (fields_any 0.00) 
