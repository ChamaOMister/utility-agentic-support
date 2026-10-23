#!/usr/bin/env bash
# One command to (re)start the demo, e.g. after a Codespace restart:
#   scripts/demo-up.sh               # checks container networking, starts the stack, runs smoke
#   scripts/demo-up.sh --fix-network # also applies the documented FORWARD-policy fix (sudo)
# See docs/troubleshooting.md for what the network fix changes.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  scripts/init-env.sh
fi

# Some Codespaces images reset the legacy iptables FORWARD policy to DROP on restart, which blocks
# container-to-container traffic (support-api and mock-billing cannot reach db).
if command -v iptables-legacy >/dev/null 2>&1; then
  policy=$(sudo -n iptables-legacy -S FORWARD 2>/dev/null | awk '$1 == "-P" {print $3}' || true)
  if [ "$policy" = "DROP" ]; then
    if [ "${1:-}" = "--fix-network" ]; then
      sudo iptables-legacy -P FORWARD ACCEPT
      echo "demo-up: FORWARD policy set to ACCEPT."
    else
      echo "demo-up: container networking is blocked (iptables-legacy FORWARD policy is DROP)." >&2
      echo "demo-up: rerun as 'scripts/demo-up.sh --fix-network' to apply:" >&2
      echo "         sudo iptables-legacy -P FORWARD ACCEPT" >&2
      exit 1
    fi
  fi
fi

if ! docker compose up -d --build --wait; then
  # A service that crashed while the network was blocked stays unhealthy; one restart recovers it.
  docker compose restart mock-billing support-api
  docker compose up -d --wait
fi
scripts/smoke.sh
mode=$(curl -fsS http://127.0.0.1:8000/api/health | python3 -c 'import json,sys; h=json.load(sys.stdin); print("live (OpenAI)" if h["live_enabled"] else "offline (scripted)")')
echo "demo-up: ready in ${mode} mode. Open the forwarded port 5173."
