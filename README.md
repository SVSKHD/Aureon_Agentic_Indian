# dhan_bridge

`dhan_bridge` is a Python service that streams Dhan market data to a Vue frontend for screeners, alerts and paper trading.

> **It never places real orders.** See [Safety](#safety).

Release **0.1.0** is the foundation: configuration, secrets handling with hot token rotation, the market calendar, the instrument master, SQLite migrations, health checks, production packaging and the Jenkins pipeline. Later releases add the live feed, candles, screeners, alerts and paper trading.

| | |
|---|---|
| Universe | NIFTY 50 (`universe/nifty50.txt`) |
| Runtime | Python 3.11, FastAPI, uvicorn (**1 worker**), asyncio, SQLite |
| Dhan SDK | `dhanhq==2.3.0` (`DhanContext` API), wrapped in `dhan_bridge/dhan_client.py` |
| Production | single Linux VPS, Docker Compose, deployed over SSH by Jenkins |

## Layout

```
dhan_bridge/
  config.py        settings (pydantic-settings), secrets file parsing, masking, SecretsStore
  calendar.py      IST helpers, market hours, NSE holidays, deploy guard CLI
  instruments.py   scrip master download + SQLite cache, symbol <-> security id
  dhan_client.py   thin read-only wrapper over dhanhq
  health.py        token monitor, /health payload, probe CLI
  keys.py          API keys (sha256-hashed in SQLite) + CLI
  auth.py          bearer-key dependency for /api, WebSocket auth helper
  backup.py        online backup / verify / restore, deploy record, instance lock
  api.py           FastAPI app (create_app factory)
  migrations/      versioned *.sql migrations + runner (with backups)
tests/             pytest suite (no network)
deploy/            compose files (prod, staging), nginx.conf, remote-deploy.sh,
                   backup-offsite.sh, systemd/ (nightly off-site backup timer)
universe/          symbol lists
holidays/          NSE holiday calendars (one JSON per year)
ci/                CI helper scripts
docs/RUNBOOK.md    operations runbook
data/              runtime state (gitignored)
```

## Local development

```bash
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt -r requirements-dev.txt

# Dev secrets live in a file, never in .env or your shell environment.
mkdir -p secrets && chmod 700 secrets
cat > secrets/dhan.env <<'EOF'
DHAN_CLIENT_ID=your-client-id
DHAN_ACCESS_TOKEN=your-access-token
EOF
openssl rand -hex 32 > secrets/admin.key
chmod 600 secrets/*

# Non-secret settings go in .env (gitignored):
cat > .env <<'EOF'
BRIDGE_MODE=replay
SECRETS_FILE=./secrets/dhan.env
ADMIN_KEY_FILE=./secrets/admin.key
EOF

make run            # uvicorn on :8000 (replay mode)
curl localhost:8000/health                       # loopback: full payload
.venv/bin/python -m dhan_bridge.keys create dev  # prints a key once
curl -H "Authorization: Bearer dbk_..." localhost:8000/api/universe
```

`make check` runs everything CI runs before the image build: ruff, ruff format, mypy (strict on `calendar.py` and `config.py`) and pytest.

### Modes

* `BRIDGE_MODE=live`: needs the secrets file. Downloads the scrip master (at most once per IST day), resolves the universe and **fails at startup if any symbol is unknown**. It verifies the token at startup and then hourly.
* `BRIDGE_MODE=replay`: never contacts Dhan. Secrets are optional. The universe is resolved only if a cached scrip master already exists. `token_ok` is `null`. Replay data arrives in a later release.

### Settings (`.env` / environment)

| Variable | Default | Notes |
|---|---|---|
| `BRIDGE_MODE` | `live` | `live` or `replay` |
| `UNIVERSE_FILE` | `universe/nifty50.txt` | one `EXCHANGE:SYMBOL` per line |
| `PAPER_CAPITAL` | `1000000` | INR |
| `SECRETS_FILE` | `/run/secrets/dhan.env` | the only source of Dhan credentials |
| `ADMIN_KEY_FILE` | `/run/secrets/admin.key` | for `POST /admin/reload-token` |
| `DATA_DIR` | `data/` | SQLite db + `backups/` |
| `HOLIDAYS_DIR` | `holidays/` | |
| `TOKEN_CHECK_INTERVAL_S` | `3600` | |
| `SECRETS_POLL_INTERVAL_S` | `5` | file-watch interval |

### Calendar CLI

```bash
python -m dhan_bridge.calendar status                          # JSON
python -m dhan_bridge.calendar deploy-guard                    # exit 0 = allowed, 3 = blocked
python -m dhan_bridge.calendar deploy-guard --at 2026-10-13T10:00
```

Deploys are blocked from 09:00 to 15:45 IST on trading days. The holiday file `holidays/nse_<year>.json` must be added each December from NSE's circular. A year without a file treats every weekday as a trading day, so the guard fails safe.

## API authentication

Every `/api/*` route requires `Authorization: Bearer <key>` and returns 401 otherwise. The routes share one FastAPI router that carries the auth dependency, and a test asserts that every `/api` path in the OpenAPI schema rejects anonymous calls.

Keys look like `dbk_...`. The `api_keys` table stores only their SHA-256, never the plaintext. The plaintext is printed once, when the key is created:

```bash
docker exec dhan_bridge python -m dhan_bridge.keys create vue-frontend   # prints the key ONCE
docker exec dhan_bridge python -m dhan_bridge.keys list                  # id, name, created, revoked
docker exec dhan_bridge python -m dhan_bridge.keys revoke 3              # takes effect immediately
```

`GET /health` is public but returns only `{"status": "ok", "version": ...}`. The full payload (`version, mode, token_ok, feed, market_open, db_ok`) needs a valid key or a **loopback TCP peer**. In practice that means `docker exec dhan_bridge python -m dhan_bridge.health probe`, which the Docker HEALTHCHECK, the smoke test and the deploy gates all use. Uvicorn runs with `--no-proxy-headers`, so an `X-Forwarded-For: 127.0.0.1` header can't fake a loopback peer. Behind docker-proxy, a host `curl 127.0.0.1:8000/health` arrives from the bridge gateway, not from loopback, so it also needs a key.

WebSockets (`/ws`, coming in P2) will use `dhan_bridge.auth.authenticate_websocket`. The client sends the key in one of two ways:

* in `Sec-WebSocket-Protocol` as `dhan-bridge.v1, bearer.<key>`. The server selects `dhan-bridge.v1` and never echoes the key back.
* as the first message: `{"type": "auth", "key": "<key>"}`. The socket closes with code 4401 if the message doesn't arrive within 5 s.

Keys in the query string (`?key=`, `?token=`, ...) are rejected with code 1008, because query strings end up in nginx access logs.

nginx rate-limits `/api` and `/ws` per client IP at 10 r/s with a burst of 20, and answers 429 beyond that. `/admin` stays 404 from outside.

## Safety

**This service must never place, modify or cancel a real order.** It holds a live brokerage token that can trade real money. A bug, a mistaken refactor or a copy-pasted SDK example must not be able to turn a screener into an order. Paper trading is simulated entirely inside the bridge.

The rule is enforced mechanically, not by convention:

1. **`tests/test_no_trade.py`** greps every non-test source file (Python, shell, YAML, SQL, Dockerfile, Jenkinsfile, and later JS/Vue) and fails if it finds any of these:
   * `place_order`, `modify_order`, `cancel_order`, `place_slice_order`
   * forever, super and conditional order functions (`place_forever`, `place_super_order`, ...)
   * position conversion, kill switch, P&L exit, eDIS/TPIN calls
   * imports of the SDK's order-capable classes, or the legacy all-in-one `dhanhq` class
   * Dhan order REST endpoints (`/orders`, `/super/orders`, `/forever/orders`, ...)

   The test also checks itself: it plants known-bad snippets and asserts the scanner catches them.
2. Jenkins runs it as a **separate hard-fail stage** ("Safety: no-trade"), not just inside the normal test run.
3. `dhan_client.py` imports only `DhanContext` and `Funds` (to read fund limits for token verification). The feed will use `dhanhq.marketfeed.MarketFeed`.
4. **Runtime guard.** Importing `dhan_client` also disarms the SDK in this process:
   * the order classes and the legacy client, on `dhanhq` and in the private modules that define them, are replaced with stubs that raise `NoTradeViolation` on any call or attribute access;
   * the SDK's HTTP layer refuses every order or account-mutating endpoint.

   `tests/test_runtime_guard.py` covers this, and it runs in the no-trade CI stage alongside the grep test.

Paper-trading code must use its own names and routes, for example `submit_paper_order` and `/api/paper/...`. These don't match the banned patterns. **Never add an exclusion to the no-trade test.** If it fires, the code is wrong.

Secrets are handled with the same care:

* Dhan credentials are read only from the secrets file. They never come from environment variables, compose files or Jenkins.
* Every `repr` and `str` of the secrets is masked. A logging filter also scrubs the raw values from every log record. Logs show a short SHA-256 fingerprint so you can confirm that a rotation took effect.
* Gitleaks scans the repository in CI.

## Migrations

Migrations live in `dhan_bridge/migrations/NNNN_name.sql` and are applied at startup, after a backup. They must be **expand-only**: never `DROP` or `RENAME` a table or column that the previous release still uses. Add new tables and nullable or defaulted columns, and stop using old ones. Remove them in a later release, once no deployable version reads them.

This is what keeps rollback cheap. The previous release keeps working against the newer schema. `remote-deploy.sh rollback` still restores the pre-deploy backup whenever the schema version changed, so data written by the new release in its first minutes is discarded on rollback.

`tests/test_migrations.py` fails on any `DROP` or `RENAME` in a migration file that lacks a line like:

```sql
-- allow-destructive: table unused since 0.3.0 (last reader removed in 0.2.0)
```

## Deployment

### First-time host setup (prod or staging)

```bash
# as root on the VPS
apt-get install -y docker.io docker-compose-plugin curl
useradd -m -G docker deploy                       # Jenkins logs in as this user
install -d -o deploy -g deploy /opt/dhan_bridge
cd /opt/dhan_bridge
install -d -o 10001 -g 10001 -m 700 data secrets  # uid 10001 = 'bridge' user in the image
install -d -m 755 certs                           # fullchain.pem + privkey.pem (e.g. certbot)

cat > .env <<'EOF'                                # compose variables (Jenkins rewrites BRIDGE_TAG)
REGISTRY=registry.example.com/aureon
BRIDGE_TAG=0.1.0
EOF
cat > bridge.env <<'EOF'                          # non-secret app settings
PAPER_CAPITAL=1000000
EOF
# staging only: BRIDGE_MODE=replay is the compose default; set BRIDGE_MODE=live in .env to override.

# registry pull credentials live on the host (Jenkins never sends them)
sudo -u deploy docker login registry.example.com
```

Add the host's SSH key to the Jenkins agent's `known_hosts`. The pipeline uses `StrictHostKeyChecking=yes`. Put the `deploy` user's public key in the `staging-ssh` / `prod-ssh` Jenkins credentials.

### Secrets file format

`/opt/dhan_bridge/secrets/dhan.env`, owned by uid 10001, mode `0400`:

```
# Dhan API credentials. KEY=VALUE, '#' comments allowed.
DHAN_CLIENT_ID=1100xxxxxx
DHAN_ACCESS_TOKEN=eyJ...
```

`/opt/dhan_bridge/secrets/admin.key` holds one line, a random string (`openssl rand -hex 32`), owned by uid 10001, mode `0400`.

The whole `secrets/` directory is mounted read-only at `/run/secrets`, so the app sees `/run/secrets/dhan.env`. The bridge mounts the directory rather than the single file. Docker pins a single-file bind mount to the old inode, so an atomic replace on the host would never become visible to the app.

### Rotating the daily token (no restart, no redeploy)

```bash
cd /opt/dhan_bridge/secrets
umask 077
printf 'DHAN_CLIENT_ID=%s\nDHAN_ACCESS_TOKEN=%s\n' "$CLIENT_ID" "$NEW_TOKEN" > dhan.env.new
chown 10001:10001 dhan.env.new && chmod 400 dhan.env.new
mv dhan.env.new dhan.env           # atomic: the app never sees a half-written file
```

The app polls the file every 5 s. When it changes, the app re-reads it, rebuilds the Dhan client and re-verifies the token immediately. The log shows `Dhan secrets loaded ... fingerprint=<new>` and `token check: ok=True`. To force a reload without waiting:

```bash
curl -fsS -X POST -H "X-Admin-Key: $(sudo cat /opt/dhan_bridge/secrets/admin.key)" \
     http://127.0.0.1:8000/admin/reload-token
# {"changed": true, "fingerprint": "...", "token_ok": true, "detail": "ok"}
```

`/admin/*` is reachable only on the host's loopback port. nginx returns 404 for it.

### Backups

* **On every app start with pending migrations:** `data/backups/<version>-<ts>.db`.
* **On every deploy:** `remote-deploy.sh deploy` takes an online backup of the *running* release before pulling anything. It records `{previous_tag, target_tag, schema_version, backup}` in `data/backups/last-deploy.json`, and `rollback` relies on that record.
* **By hand:**
  ```bash
  docker exec dhan_bridge python -m dhan_bridge.backup now
  docker exec dhan_bridge python -m dhan_bridge.backup verify <file>
  ```
  `restore` is covered in [docs/RUNBOOK.md](docs/RUNBOOK.md#4-restore-sqlite-from-a-backup). It refuses while the app is running unless you pass `--force`.

The newest 30 backups are kept. The app holds an exclusive lock on `data/bridge.lock`, so a second instance on the same data directory refuses to start.

### Off-site backups

`deploy/backup-offsite.sh` runs nightly from a systemd timer at 23:30 IST. It takes a fresh online backup, checks the newest backup with `PRAGMA integrity_check` (when `sqlite3` is installed), and copies it, together with `last-deploy.json`, off the host.

```bash
# as root on the VPS (Jenkins keeps /opt/dhan_bridge/backup-offsite.sh up to date)
install -d -m 700 /etc/dhan_bridge
cat > /etc/dhan_bridge/offsite.env <<'EOF'
OFFSITE_METHOD=rsync                                   # or: rclone
OFFSITE_TARGET=backup@vault.example.com:/srv/backups/dhan_bridge
OFFSITE_SSH_KEY=/etc/dhan_bridge/offsite_ed25519       # rsync only; add the vault to root's known_hosts
# OFFSITE_TARGET=b2:my-bucket/dhan_bridge              # rclone: configure the remote with `rclone config`
EOF
cp deploy/systemd/dhan-bridge-offsite-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now dhan-bridge-offsite-backup.timer
systemctl start dhan-bridge-offsite-backup.service && journalctl -u dhan-bridge-offsite-backup -n 20
```

Retention on the target is up to you, for example a lifecycle rule on the bucket or a `find -mtime +90 -delete` on the vault.

### What Jenkins does

`main` → lint → test → safety gates → **build** → trivy → smoke → push `:<sha>` → staging.

Tag `vX.Y.Z` → lint → test → safety gates → **promote** → trivy → smoke → push `:<version>` → approval → deploy:

* **Build once, promote:** a tag build never runs `docker build`. It pulls `:<sha>` (the image main built and staging ran) and fails with "this commit was never built on main" if that image is missing. It then retags the image as `:<version>` and pushes it.
* **Approval:** a member of `admins` must approve. The approval stage is `agent none`, so no executor is held while it waits.
* **Deploy:** the deploy stage runs on a fresh agent. It runs the market-hours guard inside the promoted image, takes the pre-deploy backup and deploys.
* **Health gate:** HTTP 200 and `db_ok`, plus, in live mode, `token_ok == true` whatever the market hours. In replay mode `token_ok` must be `null`.
* **On failure:** `remote-deploy.sh rollback`, then the health check again.

**Tag builds:** a multibranch pipeline discovers tags but does **not** build them by default. Install the **Basic Branch Build Strategies** plugin and add the build strategy "Tags" (for example, ignore tags older than 1 day). Otherwise, open the tag in Jenkins and click *Build Now*.

### Manual rollback

```bash
ssh deploy@prod
cd /opt/dhan_bridge
bash remote-deploy.sh rollback          # uses data/backups/last-deploy.json
bash remote-deploy.sh health 120 --strict
```

`rollback` compares the current schema version with the one recorded before the deploy. If they differ, it stops the bridge, restores the pre-deploy backup and then starts the previous tag. If they match, it just redeploys the previous tag. Jenkins copies `remote-deploy.sh` to `/opt/dhan_bridge/` on every deploy. To go back further than one release, see [docs/RUNBOOK.md](docs/RUNBOOK.md#6-prod-deploy-failed-and-auto-rollback-ran).

## Releasing

1. Bump `__version__` in `dhan_bridge/config.py` and add a `## [x.y.z]` section to `CHANGELOG.md`. CI fails without it.
2. Merge to `main` and wait for main's pipeline to push `:<sha>`. Then tag **that commit** `vx.y.z` (it must equal `__version__`) and push the tag.
3. Approve the prod deploy in Jenkins, outside 09:00–15:45 IST on trading days.

## Dependencies

`requirements.in` / `requirements-dev.in` are the sources. The lock files are generated with `make lock`, which runs `pip-compile --generate-hashes`. Installs always use `--require-hashes`.
