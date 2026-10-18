#!/usr/bin/env bash
# Start the factory dashboard (or bring it back after a factory rollout or a reboot), then check
# that it is reading Forge's record. Safe to run at any time: `docker compose up -d` leaves
# running containers alone and starts any that are stopped.
#
#   deploy/dashboard-up.sh            start with the current image
#   deploy/dashboard-up.sh --build    rebuild the image from this checkout first
#
# Exit status: 0 when the dashboard reports the factory's record as fresh, 1 otherwise (the
# containers are left running either way; the pages then show the stale-data banner).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
URL="${FACTORY_DASH_URL:-http://127.0.0.1:8090}"
cd "$REPO"

if [ ! -f .env ]; then
  # The feed reads its LiteLLM key from .env. Without one the dashboard still runs; only the
  # model gateway's health check is refused. Never write a key here from a script.
  echo "note: $REPO/.env is missing; creating an empty one (add FACTORY_LITELLM_KEY, see .env.example)"
  (umask 077 && : > .env)
fi
mkdir -p data

if [ "${1:-}" = "--build" ]; then
  docker compose up -d --build
else
  docker compose up -d
fi

# The feed reads Forge's record within seconds of starting.
for _ in $(seq 1 30); do
  body="$(curl -fsS -m 3 "$URL/healthz" 2>/dev/null || true)"
  case "$body" in
    *'"factory_record":"fresh"'*) echo "dashboard: up, reading the factory's record — $body"; exit 0 ;;
  esac
  sleep 2
done
echo "dashboard: containers started, but the factory's record is not fresh yet — ${body:-no answer from $URL/healthz}"
echo "  (if Forge is stopped this is expected; the dashboard catches up within 15 s of Forge starting)"
exit 1
