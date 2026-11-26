# Sift vertical scorecard (2026-11-17)

Rule-based checks (see `eval/vertical_score.py`); 1.00 is perfect.
Latency is wall time per request, cache bypassed (cache hits excluded).

| vertical | queries | score | detected | results ok | on-topic | right pages | fields | stale/empty | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|
| web | 5 | **0.92** | 1.00 | 1.00 | 0.76 | — | — | 0 | 1.1 | 4.9 |
| news | 5 | **0.88** | 1.00 | 1.00 | 1.00 | — | 0.51 | 0 | 1.4 | 2.1 |
| videos | 5 | **0.94** | 1.00 | 1.00 | 1.00 | 0.68 | 1.00 | 0 | 1.2 | 1.6 |
| people | 5 | **0.95** | 1.00 | 1.00 | 1.00 | 1.00 | 0.76 | 0 | 0.6 | 0.8 |
| social | 5 | **0.96** | 1.00 | 1.00 | 1.00 | 0.67 | 1.00 | 0 | 1.3 | 1.6 |
| jobs | 5 | **0.99** | 1.00 | 1.00 | 0.96 | — | 1.00 | 0 | 1.8 | 2.3 |
| shopping | 5 | **0.76** | 1.00 | 1.00 | 1.00 | — | 0.02 | 0 | 2.0 | 2.5 |
| places | 5 | **0.97** | 1.00 | 0.87 | — | — | 1.00 / near 1.00 | 0 | 0.5 | 1.0 |
| travel | 5 | **1.00** | 1.00 | 1.00 | 1.00 | — | — | 0 | 2.9 | 3.4 |
| academic | 5 | **0.95** | 1.00 | 1.00 | 0.84 | — | — | 0 | 1.7 | 2.1 |
| code | 5 | **0.96** | — | 1.00 | 0.92 | — | — | 0 | 2.9 | 4.2 |
| site | 5 | **0.85** | 1.00 | 0.80 | 0.90 | 0.80 | — | 1 | 0.8 | 2.0 |

**Overall: 0.93** over 60 queries; p50 1.4 s, p95 3.4 s.

## Weakest queries

- `site-05` 'site:developer.mozilla.org fetch api': 0.33 (count 0.00, url_re 0.00) searxng: searxng: ReadTimeout: 
- `web-03` 'why is the sky blue': 0.73 (must 0.20) 
- `shopping-01` 'mechanical keyboard under $100': 0.75 (fields_any 0.00) 
- `shopping-03` 'cheapest noise cancelling headphones': 0.75 (fields_any 0.00) 
- `shopping-04` 'best price for a standing desk': 0.75 (fields_any 0.00) 
- `shopping-05` 'buy running shoes for women': 0.75 (fields_any 0.00) 
- `news-02` "what's new with spacex": 0.78 (fields_any 0.10) 
- `shopping-02` 'buy keychron k2 keyboard': 0.78 (fields_any 0.10) 
- `social-03` 'hacker news discussions about sqlite': 0.80 (url_re 0.00) 
- `academic-02` 'research on crispr off-target effects': 0.80 (must 0.40) 
