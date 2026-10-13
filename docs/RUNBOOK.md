# dhan_bridge runbook

All commands run on the VPS as the `deploy` user in `/opt/dhan_bridge` unless noted otherwise. The container is called `dhan_bridge`.

Quick status:

```bash
curl -s http://127.0.0.1:8000/health; echo     # loopback, bypasses nginx
docker ps --filter name=dhan_bridge --format '{{.Image}} {{.Status}}'
docker logs --tail 100 dhan_bridge
docker exec dhan_bridge python -m dhan_bridge.calendar status
```

`/health` fields:

| Field | Meaning |
|---|---|
| `version`, `mode` | running version; `live` or `replay` |
| `token_ok` | result of the last authenticated Dhan call (`null` in replay mode) |
| `feed` | `not_started` in 0.1.0 |
| `market_open` | 09:15–15:30 IST on a trading day |
| `db_ok` | SQLite readable and migrated |

---

## 1. Token expired / `token_ok: false`

**Symptoms:** `/health` shows `"token_ok": false`. Logs show `token check: ok=False detail=dhan error DH-901 ...` (invalid or expired token) or `request failed: ...` (network problem). The prod deploy health gate fails during market hours.

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

Every startup that applies a migration first writes `data/backups/<version>-<UTC timestamp>.db`. That file is a consistent snapshot taken with SQLite's online backup API.

```bash
cd /opt/dhan_bridge
ls -1t data/backups/ | head                      # newest first; pick the one to restore
docker compose stop bridge
sudo cp data/bridge.db data/bridge.db.broken-$(date -u +%Y%m%dT%H%M%SZ)
sudo rm -f data/bridge.db-wal data/bridge.db-shm
sudo cp data/backups/<file>.db data/bridge.db
sudo chown 10001:10001 data/bridge.db
sqlite3 data/bridge.db 'PRAGMA integrity_check; SELECT * FROM schema_migrations;'   # optional
docker compose up -d bridge
curl -s http://127.0.0.1:8000/health
```

If you restore a backup taken before a migration while running the newer image, the migration is applied again on startup, and a fresh backup is taken first. To stay on the old schema, roll back the image too. See README "Manual rollback".

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

Jenkins reports `Rollback: rolled back to X (healthy)`. Production is already back on the previous image. Read `reports/` in the build artifacts and `docker logs dhan_bridge` to find the cause.

If it reports `rollback ... FAILED` or `no previous tag`, act now. Use README "Manual rollback". If the failure was in the database, also use section 4.
