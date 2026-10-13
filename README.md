# dhan_bridge

`dhan_bridge` is a Python service that streams Dhan market data to a Vue frontend for screeners, alerts and paper trading.

> **It never places real orders.** See [Safety](#safety).

Release **0.1.0** is the foundation: configuration, secrets handling with hot token rotation, the market calendar, the instrument master, PostgreSQL storage (Supabase, portable to any Postgres host), health checks, production packaging and the Jenkins pipeline. Later releases add the live feed, candles, screeners, alerts and paper trading.

| | |
|---|---|
| Universe | NIFTY 50 (`universe/nifty50.txt`) |
| Runtime | Python 3.11, FastAPI, uvicorn (**1 worker**), asyncio |
| Database | PostgreSQL (Supabase free tier, Mumbai) via SQLAlchemy 2 + psycopg 3 + Alembic; schema `bridge` |
| Dhan SDK | `dhanhq==2.3.0` (`DhanContext` API), wrapped in `dhan_bridge/dhan_client.py` |
| Production | single Linux VPS, Docker Compose, deployed over SSH by Jenkins |

## Layout

```
dhan_bridge/
  config.py        settings (pydantic-settings), secrets file parsing, masking, SecretsStore
  calendar.py      IST helpers, market hours, NSE holidays, deploy guard CLI
  db.py            engine/pool, Alembic integration, outage-tolerant Database, retry
  models.py        table definitions (schema "bridge"), the source of truth for dbverify
  migrations/      Alembic env + versions/ (revisions 0001..)
  instruments.py   scrip master download + Postgres cache (bulk COPY), symbol <-> security id
  dhan_client.py   thin read-only wrapper over dhanhq
  health.py        token monitor, /health payload, probe CLI
  keys.py          API keys (sha256-hashed) + CLI
  auth.py          bearer-key dependency for /api, WebSocket auth helper
  lock.py          single-instance lock
  api.py           FastAPI app (create_app factory)
  dbverify.py      database fingerprints (row counts, money sums, time ranges) + compare
  pgtools.py       pg_dump/pg_restore in postgres:<major>-alpine containers (host side)
  backup.py        consistent dump / verify / restore / rollback plan (host side)
  dbmove.py        move schema "bridge" to another Postgres host (host side)
tests/             pytest suite against real throwaway PostgreSQL (no network)
deploy/            compose files (prod, staging, dev), nginx.conf, remote-deploy.sh,
                   ops.sh (host DB tools), db-move.sh, backup-nightly.sh, systemd/
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

# Local PostgreSQL 16 with the production schema layout:
make dev-db
printf 'DATABASE_URL=postgresql://bridge:bridge-dev@127.0.0.1:5432/bridge?sslmode=disable\n' > secrets/db.env

# Non-secret settings go in .env (gitignored):
cp .env.example .env

make run            # uvicorn on :8000 (replay mode); migrates the dev database on start
curl localhost:8000/health                       # loopback: full payload
.venv/bin/python -m dhan_bridge.keys create dev  # prints a key once
curl -H "Authorization: Bearer dbk_..." localhost:8000/api/universe
```

`make check` runs everything CI runs before the image build: ruff, ruff format, mypy (strict on `calendar.py` and `config.py`) and pytest. The tests need Docker: they start throwaway `postgres:<major>-alpine` containers (16, plus 15 and 17 for the host-move tests). There is no SQLite fallback. To use an existing server instead, set `TEST_PG_ADMIN_URL=postgresql://postgres:...@host:5432/postgres` (each test gets its own database). Set `PG_TEST_IMAGE` to pull from a mirror, for example `mirror.gcr.io/library/postgres:{major}-alpine`.

### Modes

* `BRIDGE_MODE=live`: needs `dhan.env` and `db.env`, and **fails fast at startup if the database is unreachable**. Downloads the scrip master (at most once per IST day), resolves the universe and **fails at startup if any symbol is unknown**. It verifies the token at startup and then hourly.
* `BRIDGE_MODE=replay`: never contacts Dhan. `dhan.env` is optional. `db.env` is required, but an unreachable database doesn't stop startup: `db_ok` stays `false` and the app keeps retrying in the background. The universe is resolved only if a cached scrip master already exists. `token_ok` is `null`. Replay data arrives in a later release.

### Settings (`.env` / environment)

| Variable | Default | Notes |
|---|---|---|
| `BRIDGE_MODE` | `live` | `live` or `replay` |
| `UNIVERSE_FILE` | `universe/nifty50.txt` | one `EXCHANGE:SYMBOL` per line |
| `PAPER_CAPITAL` | `1000000` | INR |
| `SECRETS_FILE` | `/run/secrets/dhan.env` | the only source of Dhan credentials |
| `ADMIN_KEY_FILE` | `/run/secrets/admin.key` | for `POST /admin/reload-token` |
| `DB_SECRETS_FILE` | `/run/secrets/db.env` | the only source of `DATABASE_URL` (hot-reloaded) |
| `DB_SSLMODE` | `require` | applied when `DATABASE_URL` has no `sslmode`. On hosts, set it in `bridge.env` (the container and `ops.sh` both read it), never as a compose `environment:` entry, which would override `bridge.env`. |
| `HEARTBEAT_ENABLED` | `true` | daily write to `bridge.heartbeat` (keeps a free Supabase project awake) |
| `DATA_DIR` | `data/` | `backups/`, `migrations-log/`, `bridge.lock` |
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

## Database

PostgreSQL is the only store. **Python is the only process that talks to the database.** The bridge doesn't use Supabase Auth, supabase-js, PostgREST, Storage, Edge Functions or any Supabase SDK, and `tests/test_portability.py` checks the dependency locks for those SDKs. Supabase is just a managed Postgres here, and the data can move to any other Postgres host without a code change (see [Moving to another host](#moving-to-another-host)).

* **Driver:** SQLAlchemy 2 + psycopg 3, with a small pool (3 + 2 overflow, pre-ping, 10 s connect timeout).
* **Connection URL:** `DATABASE_URL` comes **only** from `/run/secrets/db.env`. Rewriting that file swaps the connection pool without a restart, the same way `dhan.env` rotation works. Passwords are masked in every log and repr.
* **Schema:** every table lives in schema **`bridge`**; nothing is created in `public`. `search_path` is set to `bridge` on every connection.
* **RLS:** row-level security is **enabled on every bridge table with no policies**, so any API role (anon, authenticated, ...) sees nothing. The bridge connects as the owner, and owners bypass RLS.
* **Types:** only standard Postgres types. Money is `NUMERIC(18,4)`; timestamps are `TIMESTAMPTZ`, stored as UTC and shown as IST. No extensions; the allowlist is empty.
* **Health:** `db_ok` means `SELECT 1` answered within 3 s **and** `alembic current == head`.
* **Outages after startup** don't crash the app. `/health` reports `db_ok: false`, `/api` answers 503 (not 401), and background writes (the scrip master refresh, the heartbeat) are logged and retried with exponential backoff.

### Supabase setup

1. **Create two projects**, `dhan-bridge-staging` and `dhan-bridge-prod`, on the free tier in region **South Asia (Mumbai) `ap-south-1`**, close to NSE and the VPS. Use a long random database password.
2. **Get the connection string** from **Connect → Session pooler** (Supavisor in session mode):
   ```
   DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require
   ```
   * **Session pooler (port 5432 on `*.pooler.supabase.com`)** is the default choice. It works over **IPv4** from any VPS.
   * **Direct connection** (`db.<project-ref>.supabase.co:5432`) also works, but it is **IPv6-only** unless you buy the IPv4 add-on. Use it if the VPS has IPv6; it saves a hop.
   * **Never use the transaction pooler** (port **6543**). Per-session state (`SET search_path`), `pg_export_snapshot()` for consistent backups and prepared statements all need a real session.
   * URL-encode special characters in the password (`@` becomes `%40`, and so on).
3. **Data API:** in **Project Settings → Data API**, do **not** add `bridge` to *Exposed schemas* (or turn the Data API off). The bridge never uses PostgREST. Even if the schema were exposed by mistake, RLS deny-all is the second line of defence.
4. Put `db.env` on the host (see [Secrets file format](#secrets-file-format)). On first start the app runs `alembic upgrade head` as the `postgres` role, which then owns schema `bridge`.
5. **The free tier pauses a project after 7 days without activity.** The app writes one heartbeat row a day (`HEARTBEAT_ENABLED=true`, on by default and set explicitly in the staging compose). Prod also writes daily through the scrip master refresh.

Migrations, schema checks and the dump tools are plain Postgres. Nothing depends on Supabase internals, and the tests run against vanilla `postgres:<major>-alpine`.

### Backups and nightly verification

Backups are **host-side tools** (`deploy/ops.sh backup ...`). `pg_dump` and `pg_restore` run in `docker run --rm postgres:<major>-alpine`, never in the bridge image. `<major>` comes from the server (`SHOW server_version_num`), so the client is never older than the server. `ops.sh` copies the Python tools out of the deployed bridge image and runs them in a hash-locked venv under `/opt/dhan_bridge/ops`; the host only needs `python3` (3.11 or newer) with `venv`, plus docker.

* **`backup now`** takes a consistent snapshot. It opens a REPEATABLE READ, READ ONLY transaction, calls `pg_export_snapshot()`, takes the [fingerprint](#fingerprints) **inside** that transaction, then runs `pg_dump -Fc --schema=bridge --snapshot=<id>`. It writes `data/backups/bridge-<ts>.dump` and `bridge-<ts>.dump.fingerprint.json`.
* **`backup verify <file>`** checks the dump's sha256, restores it into a **throwaway plain** `postgres:<major>-alpine` (nothing Supabase involved), fingerprints the result, compares it with the sidecar and deletes the container. Any difference exits non-zero.
* **`backup restore <file>`** refuses while the bridge container is running unless you pass `--force`. It first takes a safety dump (`pre-restore-*.dump`), then **renames** the current schema aside, restores, re-enables RLS and compares fingerprints. On success it drops the renamed copy. On any failure it puts the original schema back.
  * This doesn't use `pg_restore --clean --schema=bridge`: that leaves behind tables the dump doesn't know about, and can't restore into an empty database.
* **Nightly:** `deploy/backup-nightly.sh` runs from the systemd timer at 23:30 IST. It runs `now`, then `verify`, then an off-host copy (rsync over SSH or rclone), then a prune that keeps 30 dumps locally. A Discord message goes out on failure if `secrets/discord.env` has `DISCORD_WEBHOOK_URL=`. Run by hand, it works exactly the same.
* **Every deploy:** `remote-deploy.sh deploy` runs `backup now --record-deploy` **before pulling** and writes `data/backups/last-deploy.json` = `{previous_tag, target_tag, alembic_revision, backup}`.

```bash
# as root on the VPS
install -d -m 700 /etc/dhan_bridge
cat > /etc/dhan_bridge/nightly.env <<'EOF'
OFFSITE_METHOD=rsync                                   # rsync | rclone | none
OFFSITE_TARGET=backup@vault.example.com:/srv/backups/dhan_bridge
OFFSITE_SSH_KEY=/etc/dhan_bridge/offsite_ed25519       # rsync only; add the vault to root's known_hosts
# OFFSITE_TARGET=b2:my-bucket/dhan_bridge              # rclone: configure the remote with `rclone config`
EOF
cp deploy/systemd/dhan-bridge-backup-nightly.{service,timer} /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now dhan-bridge-backup-nightly.timer
systemctl start dhan-bridge-backup-nightly.service && journalctl -u dhan-bridge-backup-nightly -n 30
```

The Supabase free tier has no point-in-time recovery, so these dumps *are* the backups. Keep the off-host copy.

### Fingerprints

`dhan_bridge.dbverify.fingerprint()` records:

* the Alembic revision;
* the server major version;
* the exact row count of every table in `bridge`;
* the sum of every `NUMERIC` column and the min/max of every `TIMESTAMPTZ` column. These columns are discovered from the model metadata, so new tables are covered automatically.

Backups, `verify`, `restore` and host moves all use the same function. `compare()` lists every difference; an empty list means identical.

### Moving to another host

```bash
cd /opt/dhan_bridge
# db.env for the new host, readable by group bridge
sudo install -m 0440 -g 10001 /dev/stdin /tmp/target-db.env <<< 'DATABASE_URL=postgresql://...'
bash db-move.sh --target-secrets /tmp/target-db.env --dry-run   # full copy + verify, then removes the copy
bash db-move.sh --target-secrets /tmp/target-db.env             # the real move (outside market hours)
bash db-move.sh --revert                                        # fallback window: back to the old host
```

1. **Preflight** (nothing changes if it fails):
   * the market-hours guard (`--force-market-hours --reason "..."` to override);
   * the target is reachable and its major version is >= the source's;
   * the target schema `bridge` is absent or empty (otherwise pass `--overwrite-target`);
   * there is enough local disk for the dump.
2. **Copy:** stop the bridge (except in `--dry-run`, which copies from a live snapshot), take a consistent dump, restore it on the target and re-enable RLS.
3. **Verify:** the target's fingerprint must equal the dump's (except `server_major`), and the target's Alembic revision must be head. On a mismatch, the bridge restarts on the untouched source and the tool exits 1.
4. **Switch:** copy `db.env` to `db.env.previous`, atomically move the target's `db.env` into place, start the bridge and run the strict health gate (120 s). If the gate fails, `db.env.previous` goes back, the bridge restarts on the source and the tool exits 1.

Every run writes `data/migrations-log/<ts>.json` with hosts (no passwords), versions, both fingerprints, the duration and the result. **The source database is only ever read.** Keep it for two weeks, then delete it by hand (see the runbook).

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

Alembic revisions live in `dhan_bridge/migrations/versions/NNNN_name.py`. The app runs `alembic upgrade head` at startup, after the deploy took a pre-deploy dump. CLI inside the image: `python -m dhan_bridge.db upgrade | current | heads`.

Rules, enforced by tests:

* **Expand-only.** Never drop or rename a table or column that the previous release still uses. Add new tables and nullable or defaulted columns, and stop using old ones. Remove them in a later release, once no deployable version reads them. `tests/test_db_schema.py` fails on `op.drop_*`, `op.rename_table`, `alter_column(new_column_name=...)` or raw `DROP`/`RENAME` in `upgrade()` unless the file has a line like:
  ```python
  # -- allow-destructive: table unused since 0.3.0 (last reader removed in 0.2.0)
  ```
* **Schema and RLS.** Everything goes in schema `bridge`. Every new table needs `ALTER TABLE bridge.<t> ENABLE ROW LEVEL SECURITY` in its revision.
* **Portability.** Only standard types, money as `NUMERIC(18,4)`, `TIMESTAMPTZ` only, no extensions, and no references to Supabase schemas (`auth`, `storage`, `realtime`, `extensions`, `graphql`) or to `public`. This is checked by `tests/test_portability.py`.
* **Models and revisions agree.** `dhan_bridge/models.py` and the revisions must match: Alembic autogenerate against a migrated database must find nothing to do.
* **No downgrades.** Downgrades are not implemented. Rolling back means restoring the pre-deploy dump (`remote-deploy.sh rollback` does this when the revision moved).

Expand-only is what keeps rollback cheap: the previous release keeps working against the newer schema. `rollback` still restores the pre-deploy dump whenever the Alembic revision changed, so data written by the new release in its first minutes is discarded on rollback.

## Deployment

### First-time host setup (prod or staging)

```bash
# as root on the VPS
apt-get install -y docker.io docker-compose-plugin curl
useradd -m -G docker deploy                       # Jenkins logs in as this user
install -d -o deploy -g deploy /opt/dhan_bridge
cd /opt/dhan_bridge
apt-get install -y python3 python3-venv rsync       # host-side DB tools (ops.sh) + off-host copy
groupadd -g 10001 bridge && usermod -aG bridge deploy  # gid 10001 = the image's 'bridge' group
install -d -o deploy -g bridge -m 2770 data            # backups/, migrations-log/, bridge.lock
install -d -o deploy -g bridge -m 2750 secrets         # files 0440: readable by the app (gid 10001) and deploy
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

All secrets files are owned by `deploy:bridge` (gid 10001) with mode `0440`. The container (uid/gid 10001) can read them through the group, and so can the host DB tools that run as `deploy`.

`/opt/dhan_bridge/secrets/dhan.env`:

```
# Dhan API credentials. KEY=VALUE, '#' comments allowed.
DHAN_CLIENT_ID=1100xxxxxx
DHAN_ACCESS_TOKEN=eyJ...
```

`/opt/dhan_bridge/secrets/db.env` (see [Supabase setup](#supabase-setup)):

```
DATABASE_URL=postgresql://postgres.<project-ref>:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres?sslmode=require
```

`/opt/dhan_bridge/secrets/admin.key` holds one line, a random string (`openssl rand -hex 32`).

`/opt/dhan_bridge/secrets/discord.env` (optional, for the nightly backup alert): `DISCORD_WEBHOOK_URL=...`.

The whole `secrets/` directory is mounted read-only at `/run/secrets`, so the app sees `/run/secrets/dhan.env`. The bridge mounts the directory rather than the single file. Docker pins a single-file bind mount to the old inode, so an atomic replace on the host would never become visible to the app.

### Rotating the daily token (no restart, no redeploy)

```bash
cd /opt/dhan_bridge/secrets
umask 077
printf 'DHAN_CLIENT_ID=%s\nDHAN_ACCESS_TOKEN=%s\n' "$CLIENT_ID" "$NEW_TOKEN" > dhan.env.new
chgrp bridge dhan.env.new && chmod 440 dhan.env.new
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

See [Backups and nightly verification](#backups-and-nightly-verification). The app holds an exclusive lock on `data/bridge.lock`, so a second instance on the same host refuses to start.

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

`rollback` compares the current Alembic revision with the one recorded before the deploy. If they differ, it stops the bridge, restores the pre-deploy backup and then starts the previous tag. If they match, it just redeploys the previous tag. Jenkins copies `remote-deploy.sh` to `/opt/dhan_bridge/` on every deploy. To go back further than one release, see [docs/RUNBOOK.md](docs/RUNBOOK.md#6-prod-deploy-failed-and-auto-rollback-ran).

## Releasing

1. Bump `__version__` in `dhan_bridge/config.py` and add a `## [x.y.z]` section to `CHANGELOG.md`. CI fails without it.
2. Merge to `main` and wait for main's pipeline to push `:<sha>`. Then tag **that commit** `vx.y.z` (it must equal `__version__`) and push the tag.
3. Approve the prod deploy in Jenkins, outside 09:00–15:45 IST on trading days.

## Dependencies

`requirements.in`, `requirements-dev.in` and `requirements-ops.in` (the host-side DB tools venv) are the sources. The lock files are generated with `make lock`, which runs `pip-compile --generate-hashes`. Installs always use `--require-hashes`.
