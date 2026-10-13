#!/usr/bin/env bash
# Nightly database backup for dhan_bridge:
#   1. now      consistent pg_dump of schema "bridge" + <dump>.fingerprint.json, taken in
#               the same REPEATABLE READ snapshot (pg tools run in the postgres container)
#   2. verify   restore into a throwaway postgres:<major>-alpine, compare fingerprints;
#               any difference fails the run
#   3. offsite  copy dump + sidecar + last-deploy.json to EVERY target in
#               config/backup-targets.yaml (rsync over SSH / rclone)
#   4. prune    keep the newest $BACKUP_KEEP dumps locally (default 7)
# Any failure exits non-zero and, if a webhook is configured, posts to Discord.
#
# Usage:  scripts/backup-nightly.sh            (on the deploy host: $APP_DIR/backup-nightly.sh)
# Scheduling it (timer/cron) is part of the host's deployment wiring, not of this repo.
#
# Environment (all optional):
#   APP_DIR=/opt/dhan_bridge
#   BACKUP_KEEP=7
#   BACKUP_TARGETS_FILE=$APP_DIR/config/backup-targets.yaml
#   DISCORD_SECRETS_FILE=$APP_DIR/secrets/discord.env      (one line: DISCORD_WEBHOOK_URL=...)
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/dhan_bridge}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPS="${OPS:-$here/ops.sh}"
KEEP="${BACKUP_KEEP:-7}"
DISCORD_FILE="${DISCORD_SECRETS_FILE:-$APP_DIR/secrets/discord.env}"
step="start"

log() { echo "[backup-nightly] $(date -u +%FT%TZ) $*"; }

notify_failure() {
  local failed_step="$1" rc="$2" url=""
  [[ -r "$DISCORD_FILE" ]] && url="$(grep -s '^DISCORD_WEBHOOK_URL=' "$DISCORD_FILE" | tail -n1 | cut -d= -f2- || true)"
  [[ -n "$url" ]] || return 0
  local msg
  msg=":red_circle: **dhan_bridge nightly backup FAILED** on $(hostname) at step \`${failed_step}\` (exit ${rc}). See docs/RUNBOOK.md \"Nightly backup verification failed\"."
  # the webhook URL is a secret: it goes to curl via stdin config, never on the command line
  printf 'url = "%s"\n' "$url" | curl -fsS -m 15 -K - -H "Content-Type: application/json" \
       -d "{\"username\":\"dhan_bridge backup\",\"content\":\"${msg//\"/\\\"}\"}" >/dev/null \
    || log "WARNING: Discord notification failed"
}
trap 'rc=$?; if (( rc != 0 )); then log "FAILED at ${step} (exit ${rc})"; notify_failure "$step" "$rc"; fi' EXIT

step="now"
out="$("$OPS" backup now)"
echo "$out"
dump="$(sed -n 's/^backup=\([^ ]*\).*/\1/p' <<< "$out")"
[[ -n "$dump" ]] || { log "could not determine the dump name"; exit 1; }

step="verify"
"$OPS" backup verify "$dump"

step="offsite"
"$OPS" backup offsite "$dump"

step="prune"
"$OPS" backup prune --keep "$KEEP"

step="done"
log "OK: $dump verified, copied off-site, local copies pruned to $KEEP"
