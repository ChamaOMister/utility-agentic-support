#!/usr/bin/env bash
# Switches the running demo between the offline scripted model and live OpenAI generation.
#   scripts/set-mode.sh live      # needs OPENAI_API_KEY=... already in .env (never printed)
#   scripts/set-mode.sh offline   # back to the scripted model; the key stays in .env, unused
# Edits only LLM_PROVIDER and LIVE_LLM_ENABLED in .env, then rebuilds and recreates support-api.
# If live startup fails, it switches back to offline automatically and exits non-zero.
set -euo pipefail

cd "$(dirname "$0")/.."

mode="${1:-}"
case "$mode" in
  live | offline) ;;
  *) echo "usage: scripts/set-mode.sh live|offline" >&2; exit 2 ;;
esac

if [ ! -f .env ]; then
  echo "set-mode: .env is missing; run scripts/init-env.sh first." >&2
  exit 1
fi
chmod 600 .env # it may hold the OpenAI key

# Compose prefers shell variables over .env, which would make this script's edits ineffective.
for name in LLM_PROVIDER LIVE_LLM_ENABLED OPENAI_API_KEY OPENAI_CHAT_MODEL OPENAI_REASONING_EFFORT \
  PROJECT_BUDGET_USD CONVERSATION_BUDGET_USD; do
  if [ -n "${!name+x}" ]; then
    echo "set-mode: unset $name in this shell first (it overrides .env)." >&2
    exit 1
  fi
done

if [ "$mode" = live ] && ! grep -Eq "^OPENAI_API_KEY=[^[:space:]\"']+$" .env; then
  echo "set-mode: add OPENAI_API_KEY=<your key> to .env first (no quotes or spaces; edit the file, do not paste it anywhere else)." >&2
  exit 1
fi

set_var() {
  if grep -q "^$1=" .env; then
    sed -i "s|^$1=.*|$1=$2|" .env
  else
    printf '%s=%s\n' "$1" "$2" >> .env
  fi
}

apply() { # $1 = provider, $2 = live flag
  set_var LLM_PROVIDER "$1"
  set_var LIVE_LLM_ENABLED "$2"
  docker compose up -d --build --wait --wait-timeout 90 support-api
}

health() {
  curl -fsS http://127.0.0.1:8000/api/health 2>/dev/null |
    python3 -c 'import json,sys; h=json.load(sys.stdin); print(h["mode"], "live" if h["live_enabled"] else "offline")' 2>/dev/null
}

if [ "$mode" = live ]; then
  if ! apply openai true; then
    echo "set-mode: live startup failed; the reason is in 'docker compose logs support-api'." >&2
    echo "set-mode: switching back to offline so the demo stays up." >&2
    apply scripted false || true
    echo "set-mode: support-api is now running: $(health || echo unavailable)" >&2
    exit 1
  fi
else
  apply scripted false
fi
echo "set-mode: support-api is now running: $(health)"
