# Sift vertical scorecard (2026-11-17)

Rule-based checks (see `eval/vertical_score.py`); 1.00 is perfect.
Latency is wall time per request, cache bypassed (cache hits excluded).

| vertical | queries | score | detected | results ok | on-topic | right pages | fields | stale/empty | p50 s | p95 s |
|---|---|---|---|---|---|---|---|---|---|---|
| web | 5 | **0.91** | 1.00 | 1.00 | 0.72 | — | — | 0 | 3.5 | 5.3 |
| news | 5 | **0.73** | 1.00 | 0.60 | 1.00 | — | 0.60 | 2 | 2.1 | 3.1 |
| videos | 5 | **0.94** | 1.00 | 1.00 | 1.00 | 0.68 | 1.00 | 0 | 1.6 | 1.6 |
| people | 5 | **0.94** | 1.00 | 1.00 | 1.00 | 1.00 | 0.69 | 0 | 1.4 | 2.4 |
| social | 5 | **0.90** | 1.00 | 1.00 | 0.88 | 0.33 | 1.00 | 0 | 1.5 | 2.9 |
| jobs | 5 | **0.58** | 1.00 | 0.32 | 0.43 | — | 0.60 | 2 | 0.5 | 0.6 |
| shopping | 5 | **0.78** | 1.00 | 1.00 | 1.00 | — | 0.10 | 0 | 2.0 | 2.3 |
| places | 5 | **0.97** | 1.00 | 0.87 | — | — | 1.00 / near 1.00 | 0 | 1.1 | 3.0 |
| travel | 5 | **1.00** | 1.00 | 1.00 | 1.00 | — | — | 0 | 3.0 | 3.7 |
| academic | 5 | **0.96** | 1.00 | 1.00 | 0.88 | — | — | 0 | 3.1 | 4.3 |
| code | 5 | **0.96** | — | 1.00 | 0.92 | — | — | 0 | 3.2 | 3.5 |
| site | 5 | **0.98** | 1.00 | 1.00 | 0.92 | 1.00 | — | 0 | 1.1 | 2.1 |

**Overall: 0.89** over 60 queries; p50 1.7 s, p95 3.7 s.

## Weakest queries

- `news-04` 'latest news on the bank of canada interest rate': 0.33 (count 0.00, fields_any 0.00) searxng:news: searxng: no results, engines unavailable: bing news (timeout
- `news-05` 'recent headlines about electric vehicles': 0.33 (count 0.00, fields_any 0.00) searxng:news: searxng: no results, engines unavailable: bing news (Suspend
- `jobs-02` 'data analyst jobs in vancouver': 0.33 (count 0.00, fields_any 0.00) 
- `jobs-03` 'summer internships in software engineering': 0.33 (count 0.00, fields_any 0.00) 
- `jobs-04` 'devops engineer job openings in berlin': 0.55 (count 0.20, must 0.00) 
- `jobs-05` 'hiring frontend react developer': 0.72 (count 0.40, must 0.50) 
- `web-03` 'why is the sky blue': 0.73 (must 0.20) 
- `social-03` 'hacker news discussions about sqlite': 0.74 (must 0.40, url_re 0.30) 
- `shopping-01` 'mechanical keyboard under $100': 0.75 (fields_any 0.00) 
- `shopping-03` 'cheapest noise cancelling headphones': 0.75 (fields_any 0.00) 
