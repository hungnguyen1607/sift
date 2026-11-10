// Load test: 10 virtual users, 5 minutes, 70% repeated / 30% new queries.
// Run with the paid fallbacks disabled so it can't burn monthly quotas:
//   SIFT_ENABLED_PROVIDERS=wikipedia,stackexchange,hn,marginalia,github,openalex
//
//   k6 run eval/load_test.js
import http from "k6/http";
import { check, sleep } from "k6";

const SIFT_URL = __ENV.SIFT_URL || "http://localhost:8000";

const repeated = [
  "how do I reverse a list in python",
  "postgres jsonb vs mysql json",
  "what is an HNSW index",
  "how do I configure CORS in FastAPI",
  "what is the capital of Australia",
  "difference between merge and rebase",
  "how do I use async and await in Node.js",
];

const fresh = [
  "what is the population of Japan",
  "how do I set up SSH keys on a server",
  "what is the speed of light",
  "latest release of Kubernetes",
  "compare Redis and Memcached for caching",
  "who painted the Mona Lisa",
  "how do I write a pytest that checks an exception",
];

export const options = {
  scenarios: {
    load: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: 10 },
        { duration: "4m", target: 10 },
      ],
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
  },
};

export default function () {
  const pool = Math.random() < 0.7 ? repeated : fresh;
  const query = pool[Math.floor(Math.random() * pool.length)];
  const res = http.post(
    `${SIFT_URL}/v1/search`,
    JSON.stringify({ query, max_results: 5 }),
    { headers: { "content-type": "application/json" } }
  );
  check(res, {
    "status 200": (r) => r.status === 200,
    "has results": (r) => (r.json("results") || []).length > 0,
  });
  sleep(1);
}
