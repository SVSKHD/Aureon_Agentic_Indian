#!/usr/bin/env bash
# Nightly off-host copy of the newest dhan_bridge SQLite backup.
#
# Runs on the deploy host as root (data/ is owned by the container user), from
# deploy/systemd/dhan-bridge-offsite-backup.{service,timer}. Configuration lives in
# /etc/dhan_bridge/offsite.env (see README "Off-site backups"):
#
#   OFFSITE_METHOD=rsync            # rsync | rclone
#   OFFSITE_TARGET=backup@vault.example.com:/srv/backups/dhan_bridge   # rsync: [user@]host:/dir
#                                   # rclone: remote:bucket/dhan_bridge
#   OFFSITE_SSH_KEY=/etc/dhan_bridge/offsite_ed25519   # rsync only
#   OFFSITE_TAKE_FRESH=1            # take an online backup first (default 1)
#   APP_DIR=/opt/dhan_bridge
set -euo pipefail

CONFIG="${OFFSITE_CONFIG:-/etc/dhan_bridge/offsite.env}"
# shellcheck source=/dev/null
[[ -f "$CONFIG" ]] && source "$CONFIG"

APP_DIR="${APP_DIR:-/opt/dhan_bridge}"
BACKUPS="$APP_DIR/data/backups"
METHOD="${OFFSITE_METHOD:-rsync}"
TARGET="${OFFSITE_TARGET:?set OFFSITE_TARGET in $CONFIG}"
TAKE_FRESH="${OFFSITE_TAKE_FRESH:-1}"

log() { echo "[offsite-backup] $*"; }

if [[ "$TAKE_FRESH" == "1" ]]; then
  if [[ "$(docker inspect --format '{{.State.Running}}' dhan_bridge 2>/dev/null)" == "true" ]]; then
    log "taking a fresh online backup"
    docker exec dhan_bridge python -m dhan_bridge.backup now
  else
    log "WARNING: dhan_bridge is not running; copying the newest existing backup"
  fi
fi

newest="$(find "$BACKUPS" -maxdepth 1 -type f -name '*.db' -printf '%T@ %p\n' | sort -n | tail -n1 | cut -d' ' -f2-)"
[[ -n "$newest" ]] || { log "ERROR: no backups found in $BACKUPS"; exit 1; }

if command -v sqlite3 >/dev/null 2>&1; then
  result="$(sqlite3 "file:${newest}?mode=ro" 'PRAGMA integrity_check;')"
  [[ "$result" == "ok" ]] || { log "ERROR: $newest failed integrity_check: $result"; exit 1; }
fi

extra=()
[[ -f "$BACKUPS/last-deploy.json" ]] && extra+=("$BACKUPS/last-deploy.json")

log "copying $(basename "$newest") to $TARGET via $METHOD"
case "$METHOD" in
  rsync)
    ssh_cmd="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
    [[ -n "${OFFSITE_SSH_KEY:-}" ]] && ssh_cmd+=" -i ${OFFSITE_SSH_KEY}"
    rsync -t --partial -e "$ssh_cmd" "$newest" "${extra[@]}" "${TARGET%/}/"
    ;;
  rclone)
    rclone copy --no-traverse "$newest" "$TARGET"
    for f in "${extra[@]}"; do rclone copy --no-traverse "$f" "$TARGET"; done
    ;;
  *)
    log "ERROR: unknown OFFSITE_METHOD=$METHOD (rsync|rclone)"; exit 2 ;;
esac
log "done"
