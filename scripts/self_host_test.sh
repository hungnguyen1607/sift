#!/usr/bin/env bash
# Fresh-machine test: go from nothing to a working search, and time it.
#
#   bash scripts/self_host_test.sh
#
# Requires Docker. Pulls the prebuilt GHCR image (nothing builds locally), so
# the whole run should finish in a few minutes. Prints the total elapsed time
# and a sample search result.
set -euo pipefail

PORT="${SIFT_PORT:-8077}"
START=$(date +%s)

echo "== clone =="
tmp=$(mktemp -d)
git clone --depth 1 https://github.com/hungnguyen1607/sift "$tmp/sift"
cd "$tmp/sift"

echo "== start =="
cp .env.example .env
SIFT_PORT="$PORT" docker compose up -d

echo "== wait for api =="
for _ in $(seq 1 60); do
  if curl -fsS "http://localhost:$PORT/healthz" >/dev/null 2>&1; then break; fi
  sleep 2
done

echo "== search =="
curl -fsS "http://localhost:$PORT/v1/search" \
  -H 'content-type: application/json' \
  -d '{"query": "how do I reverse a list in python", "max_results": 3}'

END=$(date +%s)
echo ""
echo "== done in $((END - START))s =="
echo "cleanup: cd $tmp/sift && docker compose down -v"
