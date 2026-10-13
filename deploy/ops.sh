#!/usr/bin/env bash
# Host-side runner for the database ops tools:
#
#   ops.sh backup  now | verify <file> | restore <file> [--force] | rollback-plan | prune | latest
#   ops.sh dbmove  --target-secrets <db.env> [--dry-run] ...      (see db-move.sh)
#   ops.sh dbverify fingerprint
#
# Why on the host: pg_dump/pg_restore run in `docker run --rm postgres:<major>-alpine`
# containers (never in the bridge image), and the backup orchestrator must hold a
# REPEATABLE READ snapshot open while pg_dump runs, so it needs Python *and* docker.
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

image="${OPS_IMAGE:-$(docker compose --project-directory "$APP_DIR" -f "$APP_DIR/docker-compose.yml" config --images bridge)}"
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
export DB_SSLMODE="${DB_SSLMODE:-${sslmode:-require}}"
export HOLIDAYS_DIR="$code/holidays"
export BRIDGE_CONTAINER="$CONTAINER"
export PYTHONPATH="$code"
export PYTHONDONTWRITEBYTECODE=1
cd "$APP_DIR"
exec "$venv/bin/python" -m "dhan_bridge.$module" "$@"
