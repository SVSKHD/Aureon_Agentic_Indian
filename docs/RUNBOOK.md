# dhan_bridge runbook

All commands run on the VPS as the `deploy` user in `/opt/dhan_bridge` unless noted otherwise. The container is called `dhan_bridge`.

Quick status:

```bash
docker exec dhan_bridge python -m dhan_bridge.health probe --strict   # full payload (loopback peer)
docker ps --filter name=dhan_bridge --format '{{.Image}} {{.Status}}'
docker logs --tail 100 dhan_bridge
docker exec dhan_bridge python -m dhan_bridge.calendar status
```

Full `/health` fields. Public callers get only `{status, version}`. The full payload needs an API key or a loopback peer, such as `docker exec` as above.

| Field | Meaning |
|---|---|
| `version`, `mode` | running version; `live` or `replay` |
| `token_ok` | result of the last authenticated Dhan call (`null` in replay mode) |
| `feed` | `not_started` in 0.1.0 |
| `market_open` | 09:15–15:30 IST on a trading day |
| `db_ok` | SQLite readable and migrated |

---

## 1. Token expired / `token_ok: false`

**Symptoms:** `/health` shows `"token_ok": false`. Logs show `token check: ok=False detail=dhan error DH-901 ...` (invalid or expired token) or `request failed: ...` (network problem). In live mode the prod deploy health gate fails whatever the market hours, and Jenkins rolls back. Fix the token on the host before you re-run the release.

1. Find out which kind of failure it is. `dhan error DH-9xx` means the token is bad. `request failed` or `token check raised` means the network or Dhan is down; go to section 2.
2. Generate a new access token in the Dhan web portal (DhanHQ → Access Token).
3. Rotate it. No restart or redeploy is needed:
   ```bash
   cd /opt/dhan_bridge/secrets
   umask 077
   printf 'DHAN_CLIENT_ID=%s\nDHAN_ACCESS_TOKEN=%s\n' "$CLIENT_ID" "$NEW_TOKEN" > dhan.env.new
   sudo chown 10001:10001 dhan.env.new && sudo chmod 400 dhan.env.new
   sudo mv dhan.env.new dhan.env
   ```
4. Within about 5 s the log should show `Dhan secrets loaded ... fingerprint=<new>`, followed by `token check: ok=True`. To reload immediately:
   ```bash
   curl -fsS -X POST -H "X-Admin-Key: $(sudo cat secrets/admin.key)" http://127.0.0.1:8000/admin/reload-token
   ```
5. If the log shows `secrets reload skipped: secrets file is missing ...`, the file is malformed. The app keeps the old token. Fix the file and repeat.

Never paste the token into chat, tickets or Jenkins. Use the fingerprint to tell tokens apart.

## 2. Feed down / Dhan unreachable

In 0.1.0 the feed is `not_started` by design. Until the feed ships, "feed down" means Dhan REST is unreachable: `token_ok: false` with `request failed` or `token check raised`.

1. Check outbound connectivity from the container:
   ```bash
   docker exec dhan_bridge python -c "import urllib.request; print(urllib.request.urlopen('https://api.dhan.co', timeout=5).status)"
   ```
2. Check Dhan's status channels. Mid-session API outages do happen.
3. If the host's DNS or network is broken, fix it on the host. The app re-checks the token hourly. Force an immediate check with `POST /admin/reload-token`, which re-verifies even when the token hasn't changed.
4. Don't restart in a loop during market hours. Restarts don't fix upstream outages.

## 3. Disk full

**Symptoms:** `db_ok: false`, `database or disk is full` or `No space left on device` in the logs, or the container restarting.

```bash
df -h /opt/dhan_bridge /var/lib/docker
du -sh /opt/dhan_bridge/data/* | sort -h
```

1. **Old images**, usually the biggest win:
   ```bash
   docker image ls | grep dhan_bridge
   docker image prune -a --filter "until=168h"     # keeps the running image
   ```
2. **SQLite backups:** the app keeps the newest 30 in `data/backups/`. If you need space urgently, delete the oldest ones by hand. Keep at least the newest 3:
   ```bash
   ls -1tr data/backups/*.db | head -n -3
   ```
3. **Container logs** are capped by compose (5 × 20 MB per service). If an older container predates the caps, run `docker compose up -d` to recreate it.
4. Once space is free, confirm `db_ok: true`. If the database itself is damaged, go to section 4.

## 4. Restore SQLite from a backup

Backups in `data/backups/` (all consistent snapshots taken with SQLite's online backup API):

* `<version>-<UTC ts>.db`: taken before migrations, before every deploy, by the nightly off-site job, or by hand.
* `last-deploy.json`: which backup was taken before the last deploy, plus that deploy's previous tag and schema version.

`data/` belongs to the container user, so run the backup tooling **through the image** rather than with `sudo cp`:

```bash
cd /opt/dhan_bridge
run() { docker compose run --rm --no-deps -T bridge "$@"; }      # one-off container, same image/volumes/user

run ls -1t /app/data/backups | head                               # newest first
run python -m dhan_bridge.backup verify <file>.db                 # PRAGMA integrity_check
docker compose stop bridge                                        # restore refuses while the app runs
run python -m dhan_bridge.backup restore <file>.db                # keeps a safety copy of the current db
docker compose up -d bridge
docker exec dhan_bridge python -m dhan_bridge.health probe --strict
```

`restore --force` works while the app is running. Use it only if you understand that the live process keeps its open connections.

If the backup predates a migration and you keep running the newer image, the migration is re-applied on startup, after a fresh backup. To stay on the old schema, roll back the image too (`bash remote-deploy.sh rollback`, or README "Manual rollback").

**From off-site:** copy the file back into `data/backups/` with `rsync`/`rclone`, then `sudo chown 10001:10001 data/backups/<file>.db`. Verify and restore it as above.

### Off-site backup job failing

```bash
systemctl status dhan-bridge-offsite-backup.timer
journalctl -u dhan-bridge-offsite-backup -n 50
sudo /opt/dhan_bridge/backup-offsite.sh          # run once by hand
```

Common causes:

* the vault's host key is missing from root's `known_hosts`
* an expired rclone token (`rclone config reconnect <remote>:`)
* the bridge is down, so no fresh backup was taken; the job then copies the newest existing one

## 5. Deploy blocked by market hours

**Symptom:** the Jenkins "Deploy prod" stage fails with `Prod deploy blocked: 09:00-15:45 IST on a trading day`.

The block is intentional. A restart during market hours drops the live feed and resets intraday state.

* **Normal path:** wait until after 15:45 IST and re-run the build for the same tag. The guard runs after approval, so the approval and the deploy must both happen outside the window.
* **Emergency override** (for example, a hotfix for a broken production): click "Build with Parameters" on the tag build. Set `FORCE_MARKET_HOURS_DEPLOY=true` and give an `OVERRIDE_REASON`. The override and the approver are written to the build description and to the Discord message.
* Check what the guard thinks at a given moment:
  ```bash
  docker run --rm <registry>/dhan_bridge:<version> python -m dhan_bridge.calendar status
  docker run --rm <registry>/dhan_bridge:<version> python -m dhan_bridge.calendar deploy-guard --at 2026-10-13T15:50
  ```
* **Wrong holiday data** (for example, an unscheduled exchange holiday): add the date to `holidays/nse_<year>.json` and release a new version. Until that release, use the override with the reason "exchange holiday not in calendar".

## 6. Prod deploy failed and auto-rollback ran

Jenkins reports `Rollback: rolled back to X (healthy)`. Production is already back on the previous image. If the failed release had migrated the schema, the pre-deploy backup was restored. Any data written in the few minutes the new release ran is lost, by design. Read `reports/` in the build artifacts and `docker logs dhan_bridge` to find the cause.

If it reports `rollback FAILED or unhealthy`, act now:

```bash
cd /opt/dhan_bridge
run() { docker compose run --rm --no-deps -T bridge "$@"; }
run python -m dhan_bridge.backup rollback-plan     # PREVIOUS_TAG, schema versions, BACKUP, NEEDS_RESTORE
bash remote-deploy.sh rollback                     # retry; idempotent
bash remote-deploy.sh health 120 --strict
```

* **No deploy record** (`last-deploy.json` missing): set `BRIDGE_TAG=<known good>` in `.env`, then run `docker compose up -d`. If the schema moved, restore manually as in section 4.
* **Rolling back more than one release:** deploy each previous tag in turn, or restore the backup taken before the oldest of those deploys. The README "Migrations" rule (expand-only) is what makes older images keep working.
* **Health still fails on the previous tag:** the cause is probably the environment, not the release. Check the token (section 1), the network (section 2) and disk (section 3).

## 7. API key leaked or no longer needed

```bash
docker exec dhan_bridge python -m dhan_bridge.keys list
docker exec dhan_bridge python -m dhan_bridge.keys revoke <id>          # effective on the next request
docker exec dhan_bridge python -m dhan_bridge.keys create vue-frontend  # new key, printed once
```

Keys are stored only as SHA-256 hashes, so a lost key can't be recovered; revoke it and create a new one. A burst of 401s or 429s in `docker logs` for the nginx service points to a client using a stale key, or to abuse. The nginx limit is 10 r/s per IP with a burst of 20.
