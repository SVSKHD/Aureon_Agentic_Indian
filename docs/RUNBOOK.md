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
| `db_ok` | PostgreSQL answered `SELECT 1` within 3 s **and** `alembic current == head` |

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
   sudo chgrp bridge dhan.env.new && sudo chmod 440 dhan.env.new
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

## 2b. Supabase / database outage (`db_ok: false`)

**Symptoms:** `/health` shows `"db_ok": false`, `/api/*` answers **503** (`database unavailable`, `Retry-After: 30`), and the logs show `database ping failed: OperationalError ...` or `... failed (attempt n/5) ... retrying`.

After startup the app **does not crash** on a database outage. Background writes (the scrip master refresh, the heartbeat) retry with exponential backoff, and the pool reconnects by itself (`pool_pre_ping`). In live mode only **startup** needs the database, and fails fast without it.

1. **Is it Supabase or us?**
   ```bash
   cd /opt/dhan_bridge
   bash ops.sh dbverify fingerprint >/dev/null && echo "DB reachable from the host"
   docker logs --since 10m dhan_bridge | grep -iE "database|OperationalError" | tail
   ```
   Check status.supabase.com and the project dashboard. **Free projects pause after 7 days without activity:** if it says *Paused*, click *Restore*. The heartbeat should prevent this, so then also check `HEARTBEAT_ENABLED` and `docker logs dhan_bridge | grep heartbeat`.
2. **Credentials rotated?** A wrong password shows `password authentication failed`. Update `secrets/db.env` (atomic `mv`, as for `dhan.env`). The app picks it up within 5 s, no restart needed.
3. **Network:** the session pooler is IPv4 (`*.pooler.supabase.com:5432`). If you switched to the direct URL (`db.<ref>.supabase.co`), it needs IPv6 on the VPS.
4. **Wrong pooler:** `prepared statement ... does not exist`, a `search_path` problem, or snapshot errors mean someone used the **transaction pooler (port 6543)**. Switch to the session pooler (port 5432).
5. Nothing needs restarting once the database is back. `db_ok` returns to `true` on the next check. **Do not** restart the bridge in a loop during market hours.
6. **Long Supabase outage:** move to another host with the newest verified dump (section 8, *From a dump*).

## 3. Disk full

**Symptoms:** `No space left on device` in the logs or the nightly backup log, the container restarting, or `backup now` failing. The database lives on Supabase, so local disk is images, logs and dumps.

```bash
df -h /opt/dhan_bridge /var/lib/docker
du -sh /opt/dhan_bridge/data/* | sort -h
```

1. **Old images**, usually the biggest win:
   ```bash
   docker image ls | grep dhan_bridge
   docker image prune -a --filter "until=168h"     # keeps the running image
   ```
2. **Database dumps:** the nightly job keeps the newest 30 in `data/backups/`. If you need space urgently, prune harder, keeping at least the newest 3 (they are also off-host):
   ```bash
   bash ops.sh backup prune --keep 3
   ```
3. **Container logs** are capped by compose (5 × 20 MB per service). If an older container predates the caps, run `docker compose up -d` to recreate it.
4. Once space is free, re-run the nightly job by hand (`sudo ./backup-nightly.sh`) so the newest dump is verified and copied.

## 4. Restore the database from a backup

Dumps live in `data/backups/` (copies are off-host). Each `bridge-<ts>.dump` has a `<dump>.fingerprint.json` sidecar taken in the same snapshot.

| Prefix | Taken by |
|---|---|
| `bridge-` | the nightly job, a deploy (`last-deploy.json` names it), or by hand |
| `pre-restore-` | a restore, from the data it replaced |
| `move-` | `db-move.sh` |

All of this runs **on the host** through `ops.sh`. `pg_restore` runs in a `postgres:<major>-alpine` container.

```bash
cd /opt/dhan_bridge
ls -1t data/backups/*.dump | head                      # newest first
bash ops.sh backup verify bridge-<ts>.dump            # throwaway postgres + fingerprint compare
docker compose stop bridge                            # restore refuses while the bridge runs
bash ops.sh backup restore bridge-<ts>.dump           # safety dump first, then restore + verify
docker compose start bridge
bash remote-deploy.sh health 120 --strict
```

What `restore` does:

1. It writes a `pre-restore-*.dump` of the current data.
2. It renames schema `bridge` aside and restores the dump in a single transaction.
3. It re-enables RLS and checks the restored fingerprint against the sidecar.
4. It drops the renamed copy only on success. **On any failure it puts the original schema back.**

Tables created after the dump (a newer revision) do **not** survive a restore.

* **If the dump is older than the running image's head**, the app applies the newer revisions on start. To stay on the old schema, roll back the image too (`bash remote-deploy.sh rollback`).
* **`--force`** restores while the bridge is running. Avoid it: the live app keeps writing.

**From off-site:** copy the `.dump` **and** its `.fingerprint.json` back into `data/backups/` (`rsync`/`rclone`), then `verify` and `restore` as above. `verify` refuses a dump whose sha256 doesn't match its sidecar.

### Nightly backup or verify failed

You get a Discord alert (if configured) or see it in `systemctl status dhan-bridge-backup-nightly`.

```bash
journalctl -u dhan-bridge-backup-nightly -n 80
sudo /opt/dhan_bridge/backup-nightly.sh               # run once by hand; same code path
```

The log says which step failed:

* **`backup`:** the database is unreachable (section 2b), `db.env` is unreadable (secrets group/mode, see README), docker can't pull `postgres:<major>-alpine`, or the disk is full (section 3).
* **`verify` with `DIFF ...` lines:** **treat the dump as bad.**
  1. Keep it for investigation and do **not** prune by hand.
  2. Run `bash ops.sh backup now` and `bash ops.sh backup verify <new>`.
  3. If a fresh dump also differs, check the source with `bash ops.sh dbverify fingerprint`, and look for writes that bypassed the app.
  4. A `sha256 mismatch` means the file changed after it was written: disk or copy corruption.
* **`offsite`:**
  * the vault's host key is missing from root's `known_hosts`;
  * an expired rclone token (`rclone config reconnect <remote>:`);
  * the target is full.

  The local dump is still verified, so fix the copy and re-run.
* **`prune`:** harmless. The next night retries.

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

Jenkins reports `Rollback: rolled back to X (healthy)`. Production is already back on the previous image. If the failed release had moved the Alembic revision, the pre-deploy dump was restored first. Any data written in the few minutes the new release ran is lost, by design. Read `reports/` in the build artifacts and `docker logs dhan_bridge` to find the cause.

If it reports `rollback FAILED or unhealthy`, act now:

```bash
cd /opt/dhan_bridge
bash ops.sh backup rollback-plan     # PREVIOUS_TAG, RECORDED/CURRENT_REVISION, BACKUP, NEEDS_RESTORE
bash remote-deploy.sh rollback       # retry; idempotent
bash remote-deploy.sh health 120 --strict
```

* **No deploy record** (`last-deploy.json` missing): set `BRIDGE_TAG=<known good>` in `.env` and run `docker compose up -d`. If the revision moved, restore the newest pre-deploy dump as in section 4.
* **Rolling back more than one release:** deploy each previous tag in turn, or restore the dump taken before the oldest of those deploys. The expand-only rule (README "Migrations") is what lets older images run on a newer schema.
* **Health still fails on the previous tag:** the cause is probably the environment, not the release. Check the token (section 1), the database (section 2b) and disk (section 3).

## 7. API key leaked or no longer needed

```bash
docker exec dhan_bridge python -m dhan_bridge.keys list
docker exec dhan_bridge python -m dhan_bridge.keys revoke <id>          # effective on the next request
docker exec dhan_bridge python -m dhan_bridge.keys create vue-frontend  # new key, printed once
```

Keys are stored only as SHA-256 hashes, so a lost key can't be recovered; revoke it and create a new one. A burst of 401s or 429s in `docker logs` for the nginx service points to a client using a stale key, or to abuse. The nginx limit is 10 r/s per IP with a burst of 20.

## 8. Moving to another database host

Use this for leaving Supabase, changing region, or recovering from a long outage. **Outside market hours** (09:00–15:45 IST on trading days is blocked).

1. **Prepare the target:** PostgreSQL with major version >= the source's (check with `bash ops.sh dbverify fingerprint | grep server_major`), an empty database, and a role that can create schemas. No extensions are needed.
   * For a Supabase target, use the **session pooler** URL. Leave `bridge` out of the exposed Data API schemas.
   * For any other host, use `sslmode=require` and allow the VPS IP.
2. **Write the target's `db.env`** somewhere safe, readable by group bridge:
   ```bash
   sudo install -m 0440 -o deploy -g bridge /dev/stdin /opt/dhan_bridge/target-db.env <<< 'DATABASE_URL=postgresql://user:pass@new-host:5432/db?sslmode=require'
   ```
3. **Dry run.** This takes a full copy from a live snapshot and verifies it. The bridge keeps running, `db.env` is untouched, and the copy on the target is removed afterwards:
   ```bash
   cd /opt/dhan_bridge
   bash db-move.sh --target-secrets target-db.env --dry-run
   ```
   Exit 0 means the target accepts the data with identical fingerprints. Exit 2 means preflight refused (the reason is printed; nothing changed).
4. **The move.** The bridge is stopped for the copy, typically a few seconds to minutes for this data size:
   ```bash
   bash db-move.sh --target-secrets target-db.env
   ```
   * `MOVED to <host>`: done. `db.env` now points at the target, and the old one is kept as `db.env.previous`.
   * Exit 1: the copy didn't verify, or the health gate failed. The bridge is back on the **untouched source** and the differences are printed.
   * Exit 3: the fallback itself is unhealthy. Act now (section 2b).
   * Every run leaves `data/migrations-log/<ts>.json` with both fingerprints, the versions, the duration and the result.
5. **After the move:**
   * point the nightly backups at the new host (they follow `db.env` automatically);
   * run `sudo ./backup-nightly.sh` once to get a verified dump of the new host.
6. **Keep the old host for 2 weeks**, untouched, as the fallback (section 9). `db-move.sh` never modifies or deletes it. After two quiet weeks, delete it by hand: Supabase dashboard → Project settings → *Delete project*, or `DROP SCHEMA bridge CASCADE` on the old server.

**From a dump (source unreachable):** start the bridge on the new host with an empty database. Then copy the newest verified `bridge-*.dump` into `data/backups/` and run `bash ops.sh backup restore <dump>` with `db.env` pointing at the new host.

`--force-market-hours --reason "..."` overrides the guard (the reason is logged). `--overwrite-target` replaces a non-empty target schema.

## 9. Reverting a host move

Within the fallback window, if the new host misbehaves:

```bash
cd /opt/dhan_bridge
bash db-move.sh --revert
```

1. `db.env` is saved as `db.env.moved`.
2. `db.env.previous` (the old host) goes back into place.
3. The bridge restarts and the strict health gate runs.

**Writes made on the new host since the move are NOT copied back.** The tool warns about this. If they matter, take a dump of the new host first (`bash ops.sh backup now`, while `db.env` still points there) and restore the relevant data by hand. To move forward again later, run the move again (with `--overwrite-target` if the new host still has the schema).
