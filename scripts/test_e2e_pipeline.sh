#!/usr/bin/env bash
# test_e2e_pipeline.sh — End-to-end test for the embedding pipeline
#
# Prerequisites:
#   Terminal 1: make run-api       (FastAPI server on :8001)
#   Terminal 2: make run-worker    (ARQ worker)
#   Terminal 3: ./scripts/test_e2e_pipeline.sh
#
# Usage:
#   ./scripts/test_e2e_pipeline.sh              # Single evidence embed test
#   ./scripts/test_e2e_pipeline.sh --batch 10   # Batch test with 10 events
#   ./scripts/test_e2e_pipeline.sh --search     # Single search test
#   ./scripts/test_e2e_pipeline.sh --status     # Show pipeline status
#   ./scripts/test_e2e_pipeline.sh --peek       # Peek last 3 stream events

set -euo pipefail

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ---------------------------------------------------------------------------
# Config — reads from .env.dev or falls back to local defaults
# ---------------------------------------------------------------------------
ENV_FILE="${ENV_FILE:-.env.dev}"
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi

REDIS_CLI="$(which redis-cli)"
REDIS_HOST="${REDIS_HOST:-localhost}"
REDIS_PORT="${REDIS_PORT:-6379}"
REDIS_PASS="${REDIS_PASSWORD:-}"
REDIS_STREAMS_DB="${REDIS_STREAMS_DB:-3}"
STREAM_EMBED="${STREAM_EVIDENCE_EMBED:-evidence:embed}"
STREAM_SEARCH="${STREAM_EVIDENCE_SEARCH:-evidence:search}"
CONSUMER_GROUP="${STREAM_CONSUMER_GROUP:-embed-workers}"
SEARCH_GROUP="${STREAM_SEARCH_GROUP:-search-workers}"

SERVER_URL="${SERVER_URL:-http://localhost:8001}"
API_KEY="${EMBEDDING_SERVICE_API_KEY:-dev_api_key_for_local_testing}"

# Test images — local files from data/ directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INPUT_DIR="${PROJECT_DIR}/data/inputs"
REQUEST_DIR="${PROJECT_DIR}/data/request"

POLL_INTERVAL=2
POLL_TIMEOUT=120

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
BOLD='\033[1m'
DIM='\033[2m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
rcli() {
  local auth_args=()
  if [[ -n "$REDIS_PASS" ]]; then auth_args=(-a "$REDIS_PASS"); fi

  if command -v redis-cli > /dev/null 2>&1; then
    redis-cli -h "$REDIS_HOST" -p "$REDIS_PORT" "${auth_args[@]}" -n "$REDIS_STREAMS_DB" "$@" 2>/dev/null
  else
    docker exec embedding-redis redis-cli "${auth_args[@]}" -n "$REDIS_STREAMS_DB" "$@" 2>/dev/null
  fi
}

log()  { printf "${CYAN}[%s]${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }
ok()   { printf "${GREEN}[%s] ✓${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }
warn() { printf "${YELLOW}[%s] ⚠${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }
fail() { printf "${RED}[%s] ✗${NC} %s\n" "$(date '+%H:%M:%S')" "$1"; }

pipeline_status() {
  curl -sf "${SERVER_URL}/api/v1/pipeline/status" 2>/dev/null
}

get_embed_count() {
  pipeline_status | python3 -c "import sys,json; print(json.load(sys.stdin)['status_counts']['embedding'].get('$1', 0))" 2>/dev/null
}

get_search_count() {
  pipeline_status | python3 -c "import sys,json; print(json.load(sys.stdin)['status_counts']['search'].get('$1', 0))" 2>/dev/null
}

get_trigger_flushes() {
  pipeline_status | python3 -c "import sys,json; t=json.load(sys.stdin)['triggers'].get('$1',{}); print(t.get('total_flushes',0) if t else 0)" 2>/dev/null
}

get_trigger_events() {
  pipeline_status | python3 -c "import sys,json; t=json.load(sys.stdin)['triggers'].get('$1',{}); print(t.get('total_events',0) if t else 0)" 2>/dev/null
}

# Build image URLs as file:// URIs (for local testing)
get_input_urls() {
  local count=${1:-5}
  local urls=()
  for f in $(ls "$INPUT_DIR"/*.{jpg,JPG,jpeg,png} 2>/dev/null | head -n "$count"); do
    urls+=("file://${f}")
  done
  echo "${urls[@]}"
}

get_request_url() {
  local f=$(ls "$REQUEST_DIR"/*.{jpg,JPG,jpeg,png} 2>/dev/null | head -1)
  echo "file://${f}"
}

# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
preflight() {
  log "Pre-flight check..."

  # Check data directory
  if [[ ! -d "$INPUT_DIR" ]] || [[ -z "$(ls "$INPUT_DIR" 2>/dev/null)" ]]; then
    fail "No input images found in ${INPUT_DIR}"
    echo "  Place test images in data/inputs/"
    exit 1
  fi
  local input_count=$(ls "$INPUT_DIR"/*.{jpg,JPG,jpeg,png} 2>/dev/null | wc -l | tr -d ' ')
  ok "Found ${input_count} input images in data/inputs/"

  # Check server
  if ! curl -sf "${SERVER_URL}/health" > /dev/null 2>&1; then
    fail "Server not reachable at ${SERVER_URL}"
    echo "  Start the server first: make run-api"
    exit 1
  fi
  ok "Server is up"

  # Check Redis
  if ! rcli PING > /dev/null 2>&1; then
    fail "Redis not reachable at ${REDIS_HOST}:${REDIS_PORT}"
    echo "  Start Redis: make docker-up"
    exit 1
  fi
  ok "Redis is up"

  # Record initial state
  INITIAL_EMBEDDED=$(get_embed_count "embedded")
  INITIAL_DONE=$(get_embed_count "done")
  INITIAL_ERROR=$(get_embed_count "error")
  INITIAL_FLUSHES=$(get_trigger_flushes "embedding")
  INITIAL_EVENTS=$(get_trigger_events "embedding")
}

# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------
poll_embed_done() {
  local timeout=$1
  local elapsed=0
  local last_status=""
  local stale_seconds=0
  local STALE_THRESHOLD=30

  while [[ $elapsed -lt $timeout ]]; do
    sleep $POLL_INTERVAL
    elapsed=$((elapsed + POLL_INTERVAL))

    local to_work=$(get_embed_count "to_work")
    local working=$(get_embed_count "working")
    local embedded=$(get_embed_count "embedded")
    local done=$(get_embed_count "done")
    local error=$(get_embed_count "error")
    local flushes=$(get_trigger_flushes "embedding")
    local events=$(get_trigger_events "embedding")

    local new_embedded=$((embedded - INITIAL_EMBEDDED))
    local new_done=$((done - INITIAL_DONE))
    local new_err=$((error - INITIAL_ERROR))
    local new_flushes=$((flushes - INITIAL_FLUSHES))
    local new_events=$((events - INITIAL_EVENTS))

    local status_key="TW=${to_work} WK=${working} EMB=+${new_embedded} ERR=+${new_err} | flushes=${new_flushes} events=${new_events}"
    local line="  [${elapsed}s] ${status_key}"

    if [[ "$status_key" != "$last_status" ]]; then
      echo "$line"
      last_status="$status_key"
      stale_seconds=0
    else
      stale_seconds=$((stale_seconds + POLL_INTERVAL))
    fi

    # Done: no active work and at least one flush happened
    if [[ $new_flushes -gt 0 && $to_work -eq 0 && $working -eq 0 ]]; then
      break
    fi

    # Stale
    if [[ $stale_seconds -ge $STALE_THRESHOLD ]]; then
      local stuck=$((to_work + working))
      if [[ $stuck -gt 0 ]]; then
        warn "${stuck} requests still pending — safety net will clean up"
      fi
      break
    fi
  done
}

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

cmd_status() {
  printf "\n${BOLD}Pipeline Status${NC}\n"
  printf '%0.s─' {1..60}; printf "\n"
  pipeline_status | python3 -m json.tool
}

cmd_peek() {
  printf "\n${BOLD}Last 3 events in ${STREAM_EMBED}${NC}\n"
  printf '%0.s─' {1..60}; printf "\n"
  rcli XREVRANGE "$STREAM_EMBED" + - COUNT 3

  printf "\n${BOLD}Last 3 events in ${STREAM_SEARCH}${NC}\n"
  printf '%0.s─' {1..60}; printf "\n"
  rcli XREVRANGE "$STREAM_SEARCH" + - COUNT 3
}

cmd_test() {
  printf "\n${BOLD}═══════════════════════════════════════════════════${NC}\n"
  printf "${BOLD}  E2E Test: Single Evidence Embedding${NC}\n"
  printf "${BOLD}═══════════════════════════════════════════════════${NC}\n\n"

  preflight

  # Build image URL list from local files
  local image_urls_json="["
  local first=true
  for f in $(ls "$INPUT_DIR"/*.{jpg,JPG,jpeg,png} 2>/dev/null | head -5); do
    if [[ "$first" == "true" ]]; then first=false; else image_urls_json+=","; fi
    image_urls_json+="\"file://${f}\""
  done
  image_urls_json+="]"

  # Publish event
  TEST_ID="e2e-test-$(date +%s)"
  CAMERA_ID="e2e-camera-001"
  START_TIME=$(date +%s)

  log "Publishing test event: ${TEST_ID} (5 images)"
  rcli XADD "$STREAM_EMBED" '*' \
    event_type evidence.ready.embed \
    payload "{\"evidence_id\":\"${TEST_ID}\",\"camera_id\":\"${CAMERA_ID}\",\"image_urls\":${image_urls_json}}" > /dev/null
  ok "Published to ${STREAM_EMBED}"

  # Poll
  log "Waiting for pipeline (timeout: ${POLL_TIMEOUT}s)..."
  echo ""
  poll_embed_done $POLL_TIMEOUT
  echo ""

  # Results
  END_TIME=$(date +%s)
  TOTAL_TIME=$((END_TIME - START_TIME))
  FINAL_EMBEDDED=$(($(get_embed_count "embedded") - INITIAL_EMBEDDED))
  FINAL_ERROR=$(($(get_embed_count "error") - INITIAL_ERROR))
  FINAL_FLUSHES=$(($(get_trigger_flushes "embedding") - INITIAL_FLUSHES))

  printf "${BOLD}Results:${NC} ${FINAL_EMBEDDED} EMBEDDED in ${TOTAL_TIME}s | ${FINAL_FLUSHES} flushes\n"
  if [[ $FINAL_EMBEDDED -gt 0 ]]; then ok "PASS"; else fail "FAIL (${FINAL_ERROR} errors)"; fi
}

cmd_batch() {
  local BATCH_COUNT=$1
  local BATCH_TIMEOUT=$((BATCH_COUNT * 20 + 120))

  printf "\n${BOLD}═══════════════════════════════════════════════════${NC}\n"
  printf "${BOLD}  Batch Stress Test: ${BATCH_COUNT} evidence events${NC}\n"
  printf "${BOLD}═══════════════════════════════════════════════════${NC}\n\n"

  preflight

  # Build image URL list
  local image_urls_json="["
  local first=true
  for f in $(ls "$INPUT_DIR"/*.{jpg,JPG,jpeg,png} 2>/dev/null | head -5); do
    if [[ "$first" == "true" ]]; then first=false; else image_urls_json+=","; fi
    image_urls_json+="\"file://${f}\""
  done
  image_urls_json+="]"

  # Publish N events
  START_TIME=$(date +%s)
  local PUBLISHED=0
  local TEST_PREFIX="batch-$(date +%s)"

  log "Publishing ${BATCH_COUNT} events..."
  for i in $(seq 1 "$BATCH_COUNT"); do
    local eid="${TEST_PREFIX}-${i}"
    local cam="batch-cam-$(( (i % 3) + 1 ))"

    rcli XADD "$STREAM_EMBED" '*' \
      event_type evidence.ready.embed \
      payload "{\"evidence_id\":\"${eid}\",\"camera_id\":\"${cam}\",\"image_urls\":${image_urls_json}}" > /dev/null

    PUBLISHED=$((PUBLISHED + 1))
    if [[ $((PUBLISHED % 10)) -eq 0 ]]; then
      printf "  Published %d/%d events\r" "$PUBLISHED" "$BATCH_COUNT"
    fi
  done

  PUBLISH_TIME=$(($(date +%s) - START_TIME))
  ok "Published ${PUBLISHED} events in ${PUBLISH_TIME}s"

  # Poll
  log "Waiting for pipeline (timeout: ${BATCH_TIMEOUT}s)..."
  echo ""
  poll_embed_done $BATCH_TIMEOUT
  echo ""

  # Results
  END_TIME=$(date +%s)
  TOTAL_TIME=$((END_TIME - START_TIME))

  local FINAL_EMBEDDED=$(($(get_embed_count "embedded") - INITIAL_EMBEDDED))
  local FINAL_ERROR=$(($(get_embed_count "error") - INITIAL_ERROR))
  local FINAL_FLUSHES=$(($(get_trigger_flushes "embedding") - INITIAL_FLUSHES))
  local TOTAL_PROCESSED=$((FINAL_EMBEDDED + FINAL_ERROR))

  printf "${BOLD}═══════════════════════════════════════════════════${NC}\n"
  printf "${BOLD}  Batch Results${NC}\n"
  printf "${BOLD}═══════════════════════════════════════════════════${NC}\n\n"

  echo "  Events published:      ${PUBLISHED}"
  echo "  Publish time:          ${PUBLISH_TIME}s"
  echo "  Total time:            ${TOTAL_TIME}s"
  echo ""
  echo "  Requests processed:    ${TOTAL_PROCESSED}"
  echo "    EMBEDDED:            ${FINAL_EMBEDDED}"
  echo "    ERROR:               ${FINAL_ERROR}"
  echo ""
  echo "  Trigger flushes:       ${FINAL_FLUSHES}"
  echo ""

  if [[ $TOTAL_TIME -gt 0 && $TOTAL_PROCESSED -gt 0 ]]; then
    local RPS=$(python3 -c "print(f'{${TOTAL_PROCESSED} / ${TOTAL_TIME}:.2f}')")
    echo "  Throughput:            ${RPS} requests/sec"
    echo ""
  fi

  if [[ $FINAL_EMBEDDED -gt 0 ]]; then
    ok "COMPLETED — ${FINAL_EMBEDDED} embedded, ${FINAL_ERROR} errors"
  else
    fail "FAIL — no requests completed"
  fi
}

cmd_search() {
  printf "\n${BOLD}═══════════════════════════════════════════════════${NC}\n"
  printf "${BOLD}  E2E Test: Image Search${NC}\n"
  printf "${BOLD}═══════════════════════════════════════════════════${NC}\n\n"

  # Check request image
  if [[ ! -d "$REQUEST_DIR" ]] || [[ -z "$(ls "$REQUEST_DIR" 2>/dev/null)" ]]; then
    fail "No request images found in ${REQUEST_DIR}"
    exit 1
  fi

  local request_image=$(ls "$REQUEST_DIR"/*.{jpg,JPG,jpeg,png} 2>/dev/null | head -1)
  ok "Using request image: $(basename "$request_image")"

  # Check server
  if ! curl -sf "${SERVER_URL}/health" > /dev/null 2>&1; then
    fail "Server not reachable at ${SERVER_URL}"; exit 1
  fi

  # Record initial search state
  local INITIAL_SEARCH_COMPLETED=$(get_search_count "completed")

  # Publish search event
  TEST_SEARCH_ID="search-test-$(date +%s)"
  TEST_USER_ID="test-user-001"
  START_TIME=$(date +%s)

  log "Publishing search event: ${TEST_SEARCH_ID}"
  rcli XADD "$STREAM_SEARCH" '*' \
    event_type search.created \
    payload "{\"search_id\":\"${TEST_SEARCH_ID}\",\"user_id\":\"${TEST_USER_ID}\",\"image_url\":\"file://${request_image}\",\"threshold\":0.5,\"max_results\":20}" > /dev/null
  ok "Published to ${STREAM_SEARCH}"

  # Poll search completion
  log "Waiting for search to complete..."
  local elapsed=0
  while [[ $elapsed -lt 60 ]]; do
    sleep 2
    elapsed=$((elapsed + 2))
    local completed=$(get_search_count "completed")
    local new=$((completed - INITIAL_SEARCH_COMPLETED))
    local working=$(get_search_count "working")
    local to_work=$(get_search_count "to_work")
    echo "  [${elapsed}s] TW=${to_work} WK=${working} COMPLETED=+${new}"
    if [[ $new -gt 0 ]]; then break; fi
  done

  END_TIME=$(date +%s)
  TOTAL_TIME=$((END_TIME - START_TIME))
  local FINAL_COMPLETED=$(($(get_search_count "completed") - INITIAL_SEARCH_COMPLETED))

  echo ""
  printf "${BOLD}Results:${NC} ${FINAL_COMPLETED} searches completed in ${TOTAL_TIME}s\n"
  if [[ $FINAL_COMPLETED -gt 0 ]]; then ok "PASS"; else fail "FAIL"; fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
case "${1:-}" in
  --status)  cmd_status ;;
  --peek)    cmd_peek ;;
  --search)  cmd_search ;;
  --batch)
    if [[ -z "${2:-}" ]]; then
      echo "Usage: $0 --batch <count>"
      exit 1
    fi
    cmd_batch "$2"
    ;;
  --help|-h)
    echo "Usage: $0 [--status|--peek|--search|--batch N|--help]"
    echo ""
    echo "  (no args)     Single evidence embedding E2E test"
    echo "  --batch N     Batch stress test with N events"
    echo "  --search      Single image search E2E test"
    echo "  --status      Show current pipeline status"
    echo "  --peek        Show last 3 stream events"
    ;;
  *)         cmd_test ;;
esac
