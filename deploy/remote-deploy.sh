#!/usr/bin/env bash
# Runs ON the deploy host (staging or prod). Jenkins streams it over SSH:
#
#   ssh host 'bash -s -- current-tag'            < deploy/remote-deploy.sh
#   ssh host 'bash -s -- deploy 0.1.0'           < deploy/remote-deploy.sh
#   ssh host 'bash -s -- health 120 --strict'    < deploy/remote-deploy.sh
#   ssh host 'bash -s -- rollback'               < deploy/remote-deploy.sh
#
# Jenkins also copies it to $APP_DIR, so by hand:  bash /opt/dhan_bridge/remote-deploy.sh rollback
#
# deploy <tag>
#   1. online SQLite backup of the RUNNING release (before anything is pulled) and
#      data/backups/last-deploy.json = {previous_tag, target_tag, schema_version, backup}
#   2. set BRIDGE_TAG, docker compose pull && up -d
# rollback
#   reads last-deploy.json (via the image, since data/ is owned by the container user).
#   If the schema version changed since the pre-deploy backup (the new release
#   migrated), stop bridge and restore that backup first; then start previous_tag.
# backup
#   ad-hoc online backup (used by backup-offsite.sh)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dhan_bridge}"
CONTAINER="${CONTAINER:-dhan_bridge}"
cd "$APP_DIR"

compose() { docker compose --project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml" "$@"; }

# One-off container with the bridge service's image, user, volumes and env.
# Works whether or not the bridge container is running (or crash-looping).
bridge_run() { compose run --rm --no-deps -T bridge "$@"; }

is_running() { [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == "true" ]]; }

valid_tag() { [[ "$1" =~ ^[A-Za-z0-9_.-]{1,128}$ ]]; }

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
  valid_tag "$tag" || { echo "refusing suspicious tag: $tag" >&2; exit 2; }
  touch .env
  if grep -q '^BRIDGE_TAG=' .env; then
    sed -i "s/^BRIDGE_TAG=.*/BRIDGE_TAG=${tag}/" .env
  else
    echo "BRIDGE_TAG=${tag}" >> .env
  fi
}

pre_deploy_backup() {
  local previous="$1" target="$2"
  local args=(python -m dhan_bridge.backup now --record-deploy --previous-tag "$previous" --target-tag "$target")
  local rc=0
  if is_running; then
    echo "pre-deploy backup (online, from running ${previous})"
    docker exec "$CONTAINER" "${args[@]}" || rc=$?
  elif [[ -n "$previous" ]] && docker image inspect "$(compose config --images bridge)" >/dev/null 2>&1; then
    echo "pre-deploy backup (bridge not running; one-off ${previous} container)"
    bridge_run "${args[@]}" || rc=$?
  else
    echo "first deploy on this host: nothing to back up"
  fi
  case "$rc" in
    0) ;;
    3) echo "no database yet: deploying without a pre-deploy backup (rollback will need a manual tag)" ;;
    *) echo "pre-deploy backup FAILED (exit $rc): aborting deploy" >&2; exit 1 ;;
  esac
}

deploy() {
  local tag="$1" previous
  valid_tag "$tag" || { echo "refusing suspicious tag: $tag" >&2; exit 2; }
  previous="$(current_tag || true)"
  pre_deploy_backup "${previous}" "$tag"      # BEFORE pulling: aborts the deploy if it fails
  set_tag "$tag"
  echo "deploying dhan_bridge:${tag} on $(hostname) (previous: ${previous:-none})"
  compose pull bridge
  compose up -d --remove-orphans
}

rollback() {
  local plan previous="" needs_restore="" backup="" recorded="" current=""
  if ! plan="$(bridge_run python -m dhan_bridge.backup rollback-plan)"; then
    echo "rollback: no usable deploy record - roll back manually (docs/RUNBOOK.md)" >&2
    exit 1
  fi
  while IFS='=' read -r key value; do
    case "$key" in
      PREVIOUS_TAG)    previous="$value" ;;
      NEEDS_RESTORE)   needs_restore="$value" ;;
      BACKUP)          backup="$value" ;;
      RECORDED_SCHEMA) recorded="$value" ;;
      CURRENT_SCHEMA)  current="$value" ;;
    esac
  done <<< "$plan"
  valid_tag "$previous" || { echo "rollback: bad previous tag in record" >&2; exit 1; }

  if [[ "$needs_restore" == "1" ]]; then
    echo "schema changed (${recorded} -> ${current}): restoring pre-deploy backup ${backup}"
    compose stop bridge
    bridge_run python -m dhan_bridge.backup restore "$backup"
  else
    echo "schema unchanged (${current}): no restore needed"
  fi
  set_tag "$previous"
  echo "rolling back to dhan_bridge:${previous}"
  compose pull bridge
  compose up -d --remove-orphans
}

health() {
  local timeout="$1"; shift
  local deadline=$(( SECONDS + timeout ))
  while (( SECONDS < deadline )); do
    # docker exec => loopback peer => full /health payload without an API key
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
  rollback)    rollback ;;
  backup)      docker exec "$CONTAINER" python -m dhan_bridge.backup now ;;
  health)      health "${1:?usage: health <timeout-seconds> [probe args]}" "${@:2}" ;;
  *) echo "usage: $0 {current-tag|deploy <tag>|rollback|backup|health <timeout> [--strict] [--expect-version V]}" >&2; exit 2 ;;
esac
