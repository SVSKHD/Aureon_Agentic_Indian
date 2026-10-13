#!/usr/bin/env bash
# Runs ON the deploy host (staging or prod), streamed over SSH by Jenkins:
#
#   ssh host 'bash -s -- current-tag'            < deploy/remote-deploy.sh
#   ssh host 'bash -s -- deploy 0.1.0'           < deploy/remote-deploy.sh
#   ssh host 'bash -s -- health 120 --strict'    < deploy/remote-deploy.sh
#
# Also usable by hand for a manual rollback:  bash remote-deploy.sh deploy <previous-tag>
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dhan_bridge}"
CONTAINER="${CONTAINER:-dhan_bridge}"
cd "$APP_DIR"

compose() { docker compose --project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml" "$@"; }

current_tag() {
  local image
  if image="$(docker inspect --format '{{.Config.Image}}' "$CONTAINER" 2>/dev/null)"; then
    echo "${image##*:}"
  elif [[ -f .env ]] && grep -q '^BRIDGE_TAG=' .env; then
    grep '^BRIDGE_TAG=' .env | tail -n1 | cut -d= -f2-
  fi
}

set_tag() {
  local tag="$1"
  [[ "$tag" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "refusing suspicious tag: $tag" >&2; exit 2; }
  touch .env
  if grep -q '^BRIDGE_TAG=' .env; then
    sed -i "s/^BRIDGE_TAG=.*/BRIDGE_TAG=${tag}/" .env
  else
    echo "BRIDGE_TAG=${tag}" >> .env
  fi
}

deploy() {
  local tag="$1"
  set_tag "$tag"
  echo "deploying dhan_bridge:${tag} on $(hostname)"
  compose pull bridge
  compose up -d --remove-orphans
}

health() {
  local timeout="$1"; shift
  local deadline=$(( SECONDS + timeout ))
  while (( SECONDS < deadline )); do
    if docker exec "$CONTAINER" python -m dhan_bridge.health probe "$@"; then
      return 0
    fi
    sleep 5
  done
  echo "health gate failed after ${timeout}s" >&2
  docker logs --tail 50 "$CONTAINER" 2>&1 || true
  return 1
}

cmd="${1:-}"; shift || true
case "$cmd" in
  current-tag) current_tag ;;
  deploy)      deploy "${1:?usage: deploy <tag>}" ;;
  health)      health "${1:?usage: health <timeout-seconds> [probe args]}" "${@:2}" ;;
  *) echo "usage: $0 {current-tag|deploy <tag>|health <timeout> [--strict] [--expect-version V]}" >&2; exit 2 ;;
esac
