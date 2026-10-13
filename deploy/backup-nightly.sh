#!/usr/bin/env bash
# Nightly database backup for dhan_bridge:
#   1. backup now     consistent pg_dump of schema "bridge" + fingerprint sidecar
#   2. backup verify  restore into a throwaway postgres:<major>-alpine, compare fingerprints
#   3. off-host copy  rsync over SSH or rclone (OFFSITE_METHOD), dump + sidecar + last-deploy.json
#   4. prune          keep the newest $BACKUP_KEEP dumps locally (default 30)
# Any failure exits non-zero and (optionally) posts to a Discord webhook.
#
# Runs from deploy/systemd/dhan-bridge-backup-nightly.{service,timer} as root, and
# works the same when run by hand:  sudo /opt/dhan_bridge/backup-nightly.sh
#
# /etc/dhan_bridge/nightly.env (all optional):
#   OFFSITE_METHOD=rsync|rclone|none
#   OFFSITE_TARGET=backup@vault.example.com:/srv/backups/dhan_bridge   (rclone: remote:bucket/path)
#   OFFSITE_SSH_KEY=/etc/dhan_bridge/offsite_ed25519                    (rsync only)
#   BACKUP_KEEP=30
#   DISCORD_SECRETS_FILE=/opt/dhan_bridge/secrets/discord.env           (DISCORD_WEBHOOK_URL=...)
set -euo pipefail

CONFIG="${NIGHTLY_CONFIG:-/etc/dhan_bridge/nightly.env}"
# shellcheck source=/dev/null
[[ -f "$CONFIG" ]] && source "$CONFIG"

APP_DIR="${APP_DIR:-/opt/dhan_bridge}"
OPS="${OPS:-$APP_DIR/ops.sh}"
BACKUPS="$APP_DIR/data/backups"
METHOD="${OFFSITE_METHOD:-none}"
KEEP="${BACKUP_KEEP:-30}"
DISCORD_FILE="${DISCORD_SECRETS_FILE:-$APP_DIR/secrets/discord.env}"
step="start"

log() { echo "[backup-nightly] $*"; }

notify_failure() {
  local failed_step="$1" rc="$2" url=""
  [[ -r "$DISCORD_FILE" ]] && url="$(grep -s '^DISCORD_WEBHOOK_URL=' "$DISCORD_FILE" | cut -d= -f2- || true)"
  [[ -n "$url" ]] || return 0
  local host msg
  host="$(hostname)"
  msg=":red_circle: **dhan_bridge nightly backup FAILED** on ${host} at step \`${failed_step}\` (exit ${rc}). See: journalctl -u dhan-bridge-backup-nightly"
  curl -fsS -m 15 -H "Content-Type: application/json" \
       -d "{\"username\":\"dhan_bridge backup\",\"content\":\"${msg//\"/\\\"}\"}" "$url" >/dev/null \
    || log "WARNING: Discord notification failed"
}
trap 'rc=$?; if (( rc != 0 )); then log "FAILED at ${step} (exit ${rc})"; notify_failure "$step" "$rc"; fi' EXIT

step="backup"
out="$("$OPS" backup now)"
echo "$out"
dump="$(sed -n 's/^backup=\([^ ]*\).*/\1/p' <<< "$out")"
[[ -n "$dump" ]] || { log "could not determine the dump name"; exit 1; }

step="verify"
"$OPS" backup verify "$dump"

step="offsite"
files=("$BACKUPS/$dump" "$BACKUPS/$dump.fingerprint.json")
[[ -f "$BACKUPS/last-deploy.json" ]] && files+=("$BACKUPS/last-deploy.json")
case "$METHOD" in
  rsync)
    ssh_cmd="ssh -o BatchMode=yes -o StrictHostKeyChecking=yes"
    [[ -n "${OFFSITE_SSH_KEY:-}" ]] && ssh_cmd+=" -i ${OFFSITE_SSH_KEY}"
    rsync -t --partial -e "$ssh_cmd" "${files[@]}" "${OFFSITE_TARGET:?set OFFSITE_TARGET}/"
    ;;
  rclone)
    for f in "${files[@]}"; do rclone copy --no-traverse "$f" "${OFFSITE_TARGET:?set OFFSITE_TARGET}"; done
    ;;
  none)
    log "WARNING: OFFSITE_METHOD=none - the verified dump stays on this host only"
    ;;
  *) log "unknown OFFSITE_METHOD=$METHOD (rsync|rclone|none)"; exit 2 ;;
esac

step="prune"
"$OPS" backup prune --keep "$KEEP"
step="done"
log "OK: $dump verified and copied ($METHOD)"
