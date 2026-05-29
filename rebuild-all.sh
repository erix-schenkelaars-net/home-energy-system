#!/bin/bash
# rebuild-all.sh — rebuild and restart all containers found under ~/docker
# Picks up any directory containing a docker-compose.yml automatically.
# Usage:
#   ./rebuild-all.sh                              # rebuild all
#   ./rebuild-all.sh battery_optimizer read_p1   # rebuild specific ones

set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

# Auto-discover all subdirectories that have a docker-compose.yml
mapfile -t ALL < <(
  find "$DIR" -mindepth 1 -maxdepth 1 -type d \
    -exec test -f '{}/docker-compose.yml' \; -print \
  | xargs -I{} basename {} \
  | sort
)

if [[ ${#ALL[@]} -eq 0 ]]; then
  echo "No directories with docker-compose.yml found under $DIR"
  exit 1
fi

TARGETS=("${@:-${ALL[@]}}")

for svc in "${TARGETS[@]}"; do
  compose="$DIR/$svc/docker-compose.yml"
  if [[ ! -f "$compose" ]]; then
    echo "  SKIP  $svc  (no docker-compose.yml)"
    continue
  fi
  echo ""
  echo "══════════════════════════════════════════"
  echo "  BUILD  $svc"
  echo "══════════════════════════════════════════"
  docker compose --env-file "$DIR/.env" -f "$compose" down
  docker compose --env-file "$DIR/.env" -f "$compose" up -d --build
done

echo ""
echo "══════════════════════════════════════════"
echo "  All done. Running containers:"
echo "══════════════════════════════════════════"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
