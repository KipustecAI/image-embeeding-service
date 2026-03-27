#!/usr/bin/env bash
# test_local_pipeline.sh — Full pipeline test using real payload format
#
# Publishes a real evidence event to the compute service input stream,
# then watches the backend pick up the computed vectors and store them.
#
# Prerequisites:
#   1. make docker-up && make migrate
#   2. embedding-compute running (cd ../embedding-compute && make run)
#   3. Backend running: make run-api  (terminal 1)
#   4. Backend worker:  make run-worker (terminal 2)
#   5. Run this script: ./scripts/test_local_pipeline.sh
#
# It can also run standalone (without compute service) by using --mock
# which publishes pre-computed vectors directly to embeddings:results.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Load env
ENV_FILE="${PROJECT_DIR}/.env.dev"
if [[ -f "$ENV_FILE" ]]; then
  set -a; source "$ENV_FILE"; set +a
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASS="${REDIS_PASSWORD:-}"
REDIS_STREAMS_DB="${REDIS_STREAMS_DB:-3}"
STREAM_EMBEDDINGS_RESULTS="${STREAM_EMBEDDINGS_RESULTS:-embeddings:results}"

BACKEND_URL="${BACKEND_URL:-http://localhost:8001}"
PAYLOAD_FILE="${PROJECT_DIR}/data/payload_example.json"
INPUT_DIR="${PROJECT_DIR}/data/inputs"

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

# Redis helper — uses docker exec (always works with our docker-compose setup)
rcli() {
  /usr/local/bin/docker exec embedding-redis redis-cli -n "$REDIS_STREAMS_DB" "$@" 2>/dev/null
}

log()  { printf "${CYAN}[%s]${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }
ok()   { printf "${GREEN}[%s] ✓${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }
warn() { printf "${YELLOW}[%s] ⚠${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }
fail() { printf "${RED}[%s] ✗${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }

get_embed_count() {
  curl -sf "${BACKEND_URL}/api/v1/pipeline/status" 2>/dev/null | \
    python3 -c "import sys,json; print(json.load(sys.stdin)['status_counts']['embedding'].get('$1', 0))" 2>/dev/null || echo "0"
}

# ── Pre-flight ──
preflight() {
  printf "\n${BOLD}═══════════════════════════════════════════════════${NC}\n"
  printf "${BOLD}  Full Pipeline Test${NC}\n"
  printf "${BOLD}═══════════════════════════════════════════════════${NC}\n\n"

  # Check payload file
  if [[ ! -f "$PAYLOAD_FILE" ]]; then
    fail "Payload file not found: ${PAYLOAD_FILE}"
    exit 1
  fi
  ok "Payload file found"

  # Check backend
  if ! curl -sf "${BACKEND_URL}/health" > /dev/null 2>&1; then
    fail "Backend not reachable at ${BACKEND_URL}"
    echo "  Start it: make run-api + make run-worker"
    exit 1
  fi
  ok "Backend is up"

  # Check Redis
  if ! rcli PING > /dev/null 2>&1; then
    fail "Redis not reachable"
    exit 1
  fi
  ok "Redis is up"
}

# ── Test: Publish to compute input stream (full pipeline) ──
test_full_pipeline() {
  preflight

  # Parse payload
  local evidence_id=$(python3 -c "import json; d=json.load(open('${PAYLOAD_FILE}')); print(d['id'])")
  local camera_id=$(python3 -c "import json; d=json.load(open('${PAYLOAD_FILE}')); print(d['camera_id'])")
  local zip_url=$(python3 -c "import json; d=json.load(open('${PAYLOAD_FILE}')); print(d['zip_url'])")

  log "Evidence ID: ${evidence_id}"
  log "Camera ID:   ${camera_id}"
  log "ZIP URL:     ${zip_url}"

  # Build image URLs from local data/inputs (pick larger images that pass diversity filter)
  local image_urls_json
  image_urls_json=$(python3 -c "
import os, json
d = '${INPUT_DIR}'
imgs = []
for f in sorted(os.listdir(d)):
    p = os.path.join(d, f)
    if f.lower().endswith(('.jpg','.jpeg','.png')) and os.path.getsize(p) > 10000:
        imgs.append('file://' + p)
    if len(imgs) >= 5:
        break
# Fallback to any images if no large ones found
if not imgs:
    for f in sorted(os.listdir(d))[:5]:
        if f.lower().endswith(('.jpg','.jpeg','.png')):
            imgs.append('file://' + os.path.join(d, f))
print(json.dumps(imgs))
")

  local img_count=$(echo "$image_urls_json" | python3 -c "import sys,json; print(len(json.load(sys.stdin)))")
  log "Using ${img_count} local images from data/inputs/"

  # Record initial state
  local INITIAL_EMBEDDED=$(get_embed_count "embedded")
  local INITIAL_ERROR=$(get_embed_count "error")

  # Publish to evidence:embed (consumed by compute service)
  START_TIME=$(date +%s)
  log "Publishing to evidence:embed stream..."

  rcli XADD evidence:embed '*' \
    event_type evidence.ready.embed \
    payload "{\"evidence_id\":\"${evidence_id}\",\"camera_id\":\"${camera_id}\",\"image_urls\":${image_urls_json}}" > /dev/null

  ok "Published to evidence:embed"

  # Poll backend pipeline status
  log "Waiting for pipeline to complete..."
  echo ""

  local elapsed=0
  local timeout=120
  local last_status=""

  while [[ $elapsed -lt $timeout ]]; do
    sleep 2
    elapsed=$((elapsed + 2))

    local to_work=$(get_embed_count "to_work")
    local working=$(get_embed_count "working")
    local embedded=$(get_embed_count "embedded")
    local error=$(get_embed_count "error")

    local new_embedded=$((embedded - INITIAL_EMBEDDED))
    local new_error=$((error - INITIAL_ERROR))

    local status_key="TW=${to_work} WK=${working} EMB=+${new_embedded} ERR=+${new_error}"

    if [[ "$status_key" != "$last_status" ]]; then
      echo "  [${elapsed}s] ${status_key}"
      last_status="$status_key"
    fi

    # Done: something processed and no active work
    if [[ $((new_embedded + new_error)) -gt 0 && $to_work -eq 0 && $working -eq 0 ]]; then
      break
    fi
  done

  echo ""
  END_TIME=$(date +%s)
  TOTAL_TIME=$((END_TIME - START_TIME))

  local FINAL_EMBEDDED=$(($(get_embed_count "embedded") - INITIAL_EMBEDDED))
  local FINAL_ERROR=$(($(get_embed_count "error") - INITIAL_ERROR))

  # Check output streams
  local results_count=$(rcli XLEN embeddings:results 2>/dev/null || echo "0")
  log "embeddings:results stream length: ${results_count}"

  # Results
  printf "\n${BOLD}Results:${NC}\n"
  echo "  Time:      ${TOTAL_TIME}s"
  echo "  Embedded:  ${FINAL_EMBEDDED}"
  echo "  Errors:    ${FINAL_ERROR}"
  echo ""

  if [[ $FINAL_EMBEDDED -gt 0 ]]; then
    ok "PIPELINE PASS — evidence embedded end-to-end"
  elif [[ $FINAL_ERROR -gt 0 ]]; then
    warn "PARTIAL — errors occurred (check backend logs)"
  else
    # Check if compute produced results but backend didn't pick them up yet
    if [[ $results_count -gt 0 ]]; then
      warn "Compute produced results but backend hasn't consumed them yet"
      echo "  Check: is the backend running with make run-api + make run-worker?"
    else
      fail "FAIL — nothing processed. Check:"
      echo "  1. Is embedding-compute running? (cd ../embedding-compute && make run)"
      echo "  2. Is the backend running? (make run-api + make run-worker)"
    fi
  fi

  # Show final pipeline status
  echo ""
  printf "${BOLD}Pipeline Status:${NC}\n"
  curl -sf "${BACKEND_URL}/api/v1/pipeline/status" | python3 -m json.tool 2>/dev/null || echo "  (backend unreachable)"
}

# ── Test: Mock compute output (backend-only test, no GPU needed) ──
test_mock() {
  preflight

  log "Mock mode: publishing pre-computed vectors directly to embeddings:results"

  local evidence_id="mock-test-$(date +%s)"
  local camera_id="mock-cam-001"

  # Generate a fake 512-dim vector (all zeros — won't match anything, but tests the pipeline)
  local fake_vector=$(python3 -c "print([0.01]*512)")

  local INITIAL_EMBEDDED=$(get_embed_count "embedded")

  START_TIME=$(date +%s)

  # Publish directly to embeddings:results (bypass compute service)
  rcli XADD embeddings:results '*' \
    event_type embeddings.computed \
    payload "{\"evidence_id\":\"${evidence_id}\",\"camera_id\":\"${camera_id}\",\"embeddings\":[{\"image_url\":\"file:///mock/img.jpg\",\"image_index\":0,\"vector\":${fake_vector},\"total_images\":1}],\"input_count\":1,\"filtered_count\":1,\"embedded_count\":1}" > /dev/null

  ok "Published mock vectors to embeddings:results"

  # Poll
  log "Waiting for backend to store..."
  local elapsed=0
  while [[ $elapsed -lt 30 ]]; do
    sleep 2
    elapsed=$((elapsed + 2))
    local embedded=$(get_embed_count "embedded")
    local new=$((embedded - INITIAL_EMBEDDED))
    echo "  [${elapsed}s] EMBEDDED=+${new}"
    if [[ $new -gt 0 ]]; then break; fi
  done

  END_TIME=$(date +%s)
  local FINAL_EMBEDDED=$(($(get_embed_count "embedded") - INITIAL_EMBEDDED))

  echo ""
  if [[ $FINAL_EMBEDDED -gt 0 ]]; then
    ok "BACKEND PASS — stored mock vectors in Qdrant + DB (${END_TIME-START_TIME}s)"
  else
    fail "FAIL — backend didn't process the vectors"
  fi
}

# ── Main ──
case "${1:-}" in
  --mock)
    test_mock
    ;;
  --help|-h)
    echo "Usage: $0 [--mock|--help]"
    echo ""
    echo "  (no args)   Full pipeline test: evidence:embed → compute → backend"
    echo "  --mock      Backend-only test: publish mock vectors to embeddings:results"
    echo ""
    echo "Prerequisites:"
    echo "  1. make docker-up && make migrate"
    echo "  2. embedding-compute running (for full test)"
    echo "  3. make run-api + make run-worker"
    ;;
  *)
    test_full_pipeline
    ;;
esac
