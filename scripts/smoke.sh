#!/usr/bin/env bash
# Bring the Compose stack up and run every command sequence the README documents against it:
# the seeded demo state, a CLI draft and approval, the same through the HTTP API, a re-ingest,
# the regression check, and every review page. Used by CI and handy after any change.
# It expects a fresh stack: run `docker compose down -v` first when repeating it locally.
set -euo pipefail

cd "$(dirname "$0")/.."

docker compose up -d --wait
API="localhost:${API_PORT:-8090}"
REVIEW="localhost:${REVIEW_PORT:-8091}"

# MSYS_NO_PATHCONV keeps Git Bash on Windows from rewriting the container's /data paths.
cd_exec() { MSYS_NO_PATHCONV=1 docker compose exec -T api "$@"; }
# JSON is parsed inside the api container so the script does not depend on the host's Python.
jq_py() { docker compose exec -T api python -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
fail() {
  echo "smoke failed: $1"
  docker compose logs --tail 80 seed api review
  exit 1
}
count() { curl -fsS "$API/runs?status=$1" | jq_py 'len(d)'; }

echo "api: $(curl -fsS "$API/health")  review: $(curl -fsS "$REVIEW/health")"

# Seeded demo state
[ "$(count exported)" = "1" ] || fail "expected one exported run from the seed"
[ "$(count awaiting_approval)" = "2" ] || fail "expected two runs awaiting approval"
[ "$(count returned)" = "1" ] || fail "expected one returned run"
caught=$(curl -fsS "$API/stats" | jq_py 'd["citation_faults_caught"]')
[ "$caught" = "2" ] || fail "expected two citation faults caught in the seeded runs, got $caught"
changes=$(curl -fsS "$API/runs/lease-accounting-update-r2" | jq_py 'd["source_changes"][0]["now"]')
[ "$changes" = "2" ] || fail "expected the standards update to show as version 2"

# README: draft and approve from the command line
cd_exec coursedraft run revenue-recognition-refresher > /tmp/cd_run.txt
grep -q "revenue-recognition-refresher-r2  awaiting_approval" /tmp/cd_run.txt || fail "CLI run"
cd_exec coursedraft approve revenue-recognition-refresher-r1 --reviewer "Dana Whitfield" \
  > /tmp/cd_approve.txt
grep -q "exported" /tmp/cd_approve.txt || fail "CLI approve"
grep -q "deck: /data/exports/revenue-recognition-refresher-r1/v1/" /tmp/cd_approve.txt \
  || fail "CLI approve did not list the deck"
cd_exec coursedraft return revenue-recognition-refresher-r2 --reviewer "Dana Whitfield" \
  --notes "Use the Atlas contract in the LO1 discussion." > /tmp/cd_return.txt
grep -q "returned" /tmp/cd_return.txt || fail "CLI return"

# README: the same through the HTTP API
run_id=$(curl -fsS -X POST "$API/runs" -H 'content-type: application/json' \
  -d '{"course_id": "impairment-essentials"}' | jq_py 'd["id"]')
[ "$run_id" = "impairment-essentials-r2" ] || fail "API start returned $run_id"
status=$(curl -fsS -X POST "$API/runs/$run_id/decision" -H 'content-type: application/json' \
  -d '{"decision": "approved", "reviewer": "Dana Whitfield", "notes": "Checked the case figures."}' \
  | jq_py 'd["status"]')
[ "$status" = "exported" ] || fail "API decision left the run $status"
export_id=$(curl -fsS "$API/runs/$run_id" | jq_py '[e["id"] for e in d["exports"] if e["kind"]=="deck"][0]')
curl -fsS -o /tmp/cd_deck.pptx "$API/exports/$export_id/download"
[ "$(head -c 2 /tmp/cd_deck.pptx)" = "PK" ] || fail "downloaded deck is not a pptx"
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$API/runs/$run_id/decision" \
  -H 'content-type: application/json' -d '{"decision": "approved", "reviewer": "x"}')
[ "$code" = "409" ] || fail "a second decision should be refused, got $code"

# README: re-ingesting an unchanged file is a no-op
cd_exec coursedraft ingest \
  /data/sources/lease-accounting-update/lease-accounting-update-standards-update.docx \
  --course lease-accounting-update --type standard > /tmp/cd_ingest.txt
grep -q "unchanged" /tmp/cd_ingest.txt || fail "re-ingest"

# README: the regression check
cd_exec coursedraft eval > /tmp/cd_eval.txt || { cat /tmp/cd_eval.txt; fail "eval"; }
grep -q "^PASS" /tmp/cd_eval.txt || fail "eval did not pass"

# Review pages
for path in / /runs/lease-accounting-update-r2 /runs/"$run_id" /sources /audit /chunks/S3.5; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$REVIEW$path")
  [ "$code" = "200" ] || fail "review page $path returned $code"
done
curl -fsS "$REVIEW/runs/lease-accounting-update-r2" > /tmp/cd_review.html
grep -q "Checks before review" /tmp/cd_review.html || fail "review page content"
grep -q "redrafted, passed" /tmp/cd_review.html || fail "review page should show the redraft"

echo "smoke passed"
