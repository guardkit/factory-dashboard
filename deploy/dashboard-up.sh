#!/usr/bin/env bash
# Start the factory dashboard (or bring it back after a factory rollout or a reboot), then check
# that it is reading Forge's record. Safe to run at any time: `docker compose up -d` leaves
# running containers alone and starts any that are stopped.
#
#   deploy/dashboard-up.sh            start with the current image
#   deploy/dashboard-up.sh --build    rebuild the image from this checkout first
#
# Exit status: 0 when the dashboard reports the factory's record as fresh; 1 when it started but the
# record is not fresh yet (the pages then show the stale-data banner); 2 when the board's
# configuration folder (accounts and session secret) is missing, so nothing was started.
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

# The board reads its accounts and session secret from a folder outside the repository (README,
# "Accounts and access"); compose will not start it without the folder, and the board will not
# start without both files. Say so plainly rather than leave a half-started dashboard.
CONFIG="${FACTORY_DASH_CONFIG_DIR:-$HOME/.config/factory-dashboard}"
for need in session_secret accounts.yaml; do
  if [ ! -r "$CONFIG/$need" ]; then
    echo "dashboard: not started — $CONFIG/$need is missing (see README.md, 'Accounts and access')"
    exit 2
  fi
done
export FACTORY_DASH_CONFIG_DIR="$CONFIG"

# The Linear door's state volume (each project's Linear link) is mounted only when it exists:
# compose refuses to start anything when an external volume is missing (docker-compose.linear.yml).
# The name is the one compose itself resolves (shell, then .env, then the default).
FILES=(-f docker-compose.yml)
if ! LINEAR_VOLUME="$(deploy/linear-volume.sh)" || [ -z "$LINEAR_VOLUME" ]; then
  echo "note: could not read the Linear door's volume name from compose; starting without Linear links"
elif docker volume inspect "$LINEAR_VOLUME" >/dev/null 2>&1; then
  FILES+=(-f docker-compose.linear.yml)
else
  echo "note: the Linear door's volume $LINEAR_VOLUME is not there; starting without Linear links"
fi

if [ "${1:-}" = "--build" ]; then
  docker compose "${FILES[@]}" up -d --build
else
  docker compose "${FILES[@]}" up -d
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
