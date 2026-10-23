#!/usr/bin/env bash
# Compose health + host HTTP readiness, bounded by one shared 120-second deadline.
set -euo pipefail
cd "$(dirname "$0")/.."

budget=${SMOKE_TIMEOUT_SECONDS:-120}
if [[ ! "$budget" =~ ^[0-9]+$ ]] || ((budget < 1 || budget > 120)); then
  echo 'smoke: SMOKE_TIMEOUT_SECONDS must be an integer from 1 to 120' >&2
  exit 1
fi
SECONDS=0

bounded() {
  local remaining=$((budget - SECONDS))
  ((remaining > 0)) || return 1
  timeout "${remaining}s" "$@"
}

container_healthy() {
  local id health
  id=$(bounded docker compose ps -q "$1" 2>/dev/null) || return 1
  [[ -n "$id" ]] || return 1
  health=$(bounded docker inspect --format '{{.State.Health.Status}}' "$id" 2>/dev/null) || return 1
  [[ "$health" == healthy ]]
}

http_ready() {
  local binding port status remaining
  binding=$(bounded docker compose port "$1" "$2" 2>/dev/null) || return 1
  port=${binding##*:}
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  remaining=$((budget - SECONDS))
  ((remaining > 0)) || return 1
  ((remaining <= 3)) || remaining=3
  status=$(bounded curl --silent --output /dev/null --write-out '%{http_code}' \
    --max-time "$remaining" "http://127.0.0.1:${port}$3") || return 1
  [[ "$status" == 200 ]]
}

while ((SECONDS < budget)); do
  if container_healthy db &&
    bounded docker compose exec -T db pg_isready -h 127.0.0.1 -U postgres -d uas >/dev/null 2>&1 &&
    container_healthy mock-billing && http_ready mock-billing 8001 /health &&
    container_healthy support-api && http_ready support-api 8000 /api/health &&
    container_healthy web && http_ready web 5173 /; then
    echo 'smoke: db healthy (pg_isready); mock-billing HTTP 200; support-api HTTP 200; web HTTP 200'
    exit 0
  fi
  ((SECONDS >= budget)) || sleep 1
done

echo "smoke: stack not ready within ${budget}s; inspect docker compose ps and service logs" >&2
exit 1
