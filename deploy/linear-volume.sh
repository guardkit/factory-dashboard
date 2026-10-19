#!/usr/bin/env bash
# Print the name of the Linear door's state volume exactly as compose resolves it for
# docker-compose.linear.yml: FACTORY_LINEAR_DOOR_VOLUME from the shell, else from ./.env, else
# forge-estate_linear-door-state. Asking compose itself keeps its precedence rules, so the name
# dashboard-up.sh checks is the name compose mounts. Exit status 1 when compose cannot say.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

docker compose -f docker-compose.yml -f docker-compose.linear.yml config --format json 2>/dev/null |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["volumes"]["linear-door-state"]["name"])'
