#!/usr/bin/env bash
# Host-side runner for the database ops tools:
#
#   ops.sh backup  now | verify <file> | restore <file> [--force] | offsite <file>
#                  | rollback-plan | prune [--keep N] | latest
#   ops.sh dbmove  --target-secrets <db.env> [--dry-run] ...      (see db-move.sh)
#   ops.sh dbverify compare <a.json> <b.json>
#
# The host never connects to PostgreSQL (the compose database has no published
# port). The Python tools orchestrate; psql / pg_dump / pg_restore run inside
# containers: `docker compose exec -T postgres ...` for our own database, and
# `docker run --rm postgres:<major>-alpine ...` for any other host (db-move targets).
# Neither the host nor the bridge image needs PostgreSQL client tools.
#
# The Python code is copied out of the CURRENTLY CONFIGURED bridge image (BRIDGE_TAG
# in .env), so host tools always match the deployed release. Dependencies come from
# that image's requirements-ops.txt (hash-locked) in a venv under $APP_DIR/ops.
# Requirements on the host: python3 (>= 3.11) with venv, docker.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dhan_bridge}"
OPS_DIR="${OPS_DIR:-$APP_DIR/ops}"
PYTHON="${OPS_PYTHON:-python3}"
CONTAINER="${CONTAINER:-dhan_bridge}"

module="${1:?usage: ops.sh <backup|dbmove|dbverify> [args...]}"; shift
case "$module" in backup|dbmove|dbverify) ;; *) echo "unknown ops module: $module" >&2; exit 2 ;; esac

# exactly the bridge service's image (`config --images bridge` would add postgres: depends_on)
image="${OPS_IMAGE:-$(docker compose --project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml" \
  config --format json | "$PYTHON" -c 'import json, sys; print(json.load(sys.stdin)["services"]["bridge"]["image"])')}"
tag="${image##*:}"
code="$OPS_DIR/code/$tag"
if [[ ! -f "$code/.complete" ]]; then
  rm -rf "$code" && mkdir -p "$code"
  tmp="dhan-ops-extract-$$"
  docker create --name "$tmp" "$image" >/dev/null
  trap 'docker rm -f "$tmp" >/dev/null 2>&1 || true' EXIT
  docker cp "$tmp:/app/dhan_bridge" "$code/dhan_bridge"
  docker cp "$tmp:/app/holidays" "$code/holidays"
  docker cp "$tmp:/app/ops/requirements-ops.txt" "$code/requirements-ops.txt"
  docker rm -f "$tmp" >/dev/null; trap - EXIT
  touch "$code/.complete"
fi

lock_hash="$(sha256sum "$code/requirements-ops.txt" | cut -c1-12)"
venv="$OPS_DIR/venv-$lock_hash"
if [[ ! -x "$venv/bin/python" ]]; then
  "$PYTHON" -m venv "$venv.tmp"
  "$venv.tmp/bin/pip" install --quiet --disable-pip-version-check --require-hashes \
      -r "$code/requirements-ops.txt"
  mv "$venv.tmp" "$venv"
fi

# Same settings the bridge uses, but with host paths.
sslmode="$(grep -s '^DB_SSLMODE=' "$APP_DIR/bridge.env" | tail -n1 | cut -d= -f2- || true)"
export DATA_DIR="$APP_DIR/data"
export DB_SECRETS_FILE="${DB_SECRETS_FILE:-$APP_DIR/secrets/db.env}"
export DB_SSLMODE="${DB_SSLMODE:-${sslmode:-prefer}}"
export HOLIDAYS_DIR="$code/holidays"
export BRIDGE_CONTAINER="$CONTAINER"
# DATABASE_URL host == DB_SERVICE -> our compose database: tools via `compose exec`
export DB_SERVICE="${DB_SERVICE:-postgres}"
# shellcheck disable=SC2016  # single quotes are inside double quotes: they DO expand (quoting for shlex)
export DB_EXEC_CMD="${DB_EXEC_CMD:-docker compose --project-directory '$APP_DIR' -f '$APP_DIR/docker-compose.yml' exec -T}"
export DB_EXEC_TARGET="${DB_EXEC_TARGET:-postgres}"
export BACKUP_TARGETS_FILE="${BACKUP_TARGETS_FILE:-$APP_DIR/config/backup-targets.yaml}"
export PYTHONPATH="$code"
export PYTHONDONTWRITEBYTECODE=1
cd "$APP_DIR"
exec "$venv/bin/python" -m "dhan_bridge.$module" "$@"
