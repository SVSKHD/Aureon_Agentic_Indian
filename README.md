# dhan_bridge

`dhan_bridge` is a Python service that streams Dhan market data to a Vue frontend for screeners, alerts and paper trading.

> **It never places real orders.** See [Safety](#safety).

Release **0.1.0** is the foundation: configuration, secrets handling with hot token rotation, the market calendar, the instrument master, PostgreSQL 16 storage in the compose stack (portable to any Postgres host), backup verification and host-migration tooling, health checks, production packaging and the Jenkins pipeline. Later releases add the live feed, candles, screeners, alerts and paper trading.

| | |
|---|---|
| Universe | NIFTY 50 (`universe/nifty50.txt`) |
| Runtime | Python 3.11, FastAPI, uvicorn (**1 worker**), asyncio |
| Database | PostgreSQL 16 (compose service, internal network) via SQLAlchemy 2 + psycopg 3 + Alembic; schema `bridge` |
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
  cors.py          exact-origin CORS for /api, Origin check for WebSockets
  pgtools.py       psql/pg_dump/pg_restore in postgres containers (host side, no pg tools needed)
  backup.py        consistent dump / verify / restore / off-site copy / rollback plan (host side)
  dbmove.py        move schema "bridge" to another Postgres host (host side)
tests/             pytest suite against real throwaway PostgreSQL (no network)
deploy/            compose files (prod, staging, dev), postgresql.conf, nginx.conf, remote-deploy.sh
scripts/           ops.sh (host DB tools), backup-nightly.sh, db-move.sh
config/            backup-targets.yaml (commented example of off-site targets)
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

# Local PostgreSQL 16: the production postgres service, published on loopback:
make dev-db
printf 'DATABASE_URL=postgresql://bridge:bridge-dev@127.0.0.1:5432/bridge?sslmode=disable\n' > secrets/db.env

# Non-secret settings go in .env (gitignored):
cp .env.example .env

make run            # uvicorn on :8000 (replay mode); migrates the dev database on start
curl localhost:8000/health                       # loopback: full payload
.venv/bin/python -m dhan_bridge.keys create dev  # prints a key once
curl -H "Authorization: Bearer dbk_..." localhost:8000/api/universe
```

`make check` runs everything CI runs before the image build: ruff, ruff format, mypy (strict on `calendar.py` and `config.py`) and pytest. The tests need Docker and run against **real PostgreSQL only** (no SQLite fallback): they start throwaway `postgres:<major>-alpine` containers (16, plus 15 and 17 for the host-move tests), reach "our" database with `docker exec` like production, and other servers through `docker run --network host` tools containers. Set `PG_TEST_IMAGE` to pull from a mirror, for example `mirror.gcr.io/library/postgres:{major}-alpine`.

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
| `DB_SSLMODE` | `prefer` | applied when `DATABASE_URL` has no `sslmode`. On hosts, set it in `bridge.env` (the container and `ops.sh` both read it), never as a compose `environment:` entry, which would override `bridge.env`. |
| `ALLOWED_ORIGINS` | (empty) | comma-separated **exact** browser origins (`https://app.example.org,http://localhost:5173`) for `/api` CORS and the WebSocket Origin check. No paths, no wildcards; `*` is refused in live mode. |
| `BACKUP_KEEP` | `7` | local dumps kept by `backup prune` / the nightly job (ops tools) |
| `BACKUP_TARGETS_FILE` | `config/backup-targets.yaml` | off-site targets (ops tools) |
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

PostgreSQL 16 is the only store, and it runs **in our own compose stack** (service `postgres`). **Python is the only process that talks to the database**: no other service, SDK or API layer connects to it. The data can move to any other PostgreSQL host later without a code change (see [Moving to another host](#moving-to-another-host)).

### Architecture

```
            compose network "default"                 compose network "db" (internal: true)
 nginx ──────────────► bridge (FastAPI, 1 worker) ─────────────► postgres:16-alpine (pinned by digest)
                         │  DATABASE_URL from /run/secrets/db.env          │  no published port
                         │  pool 3 + 2, search_path = bridge                │  volume pgdata
                         ▼                                                  │  deploy/postgresql.conf
                     Dhan APIs                    host: scripts/ops.sh ─────┘  (docker compose exec -T postgres ...)
```

* **Driver:** SQLAlchemy 2 + psycopg 3, with a small pool (3 + 2 overflow, pre-ping, 10 s connect timeout). `sslmode` comes from `DB_SSLMODE` (default `prefer`) unless the URL sets it; use `verify-full` for a remote host with a proper certificate.
* **Connection URL:** `DATABASE_URL` comes **only** from `/run/secrets/db.env`. Rewriting that file swaps the connection pool without a restart, the same way `dhan.env` rotation works. Passwords are masked in every log, repr and migration log.
* **Compose service:** `postgres:16-alpine` pinned by digest; named volume `pgdata`; **no published port**, attached only to the internal network `db` (the bridge is on both networks). The superuser password comes from `secrets/postgres_password` (`POSTGRES_PASSWORD_FILE`); `db.env` holds the same password. A `pg_isready` healthcheck gates the bridge (`depends_on: service_healthy`). `deploy/postgresql.conf` is mounted read-only: `shared_buffers=256MB`, `max_connections=30`, UTC. Memory and CPU limits are compose variables with defaults: `PG_MEM_LIMIT=1g`, `PG_CPUS=1.0`, `BRIDGE_MEM_LIMIT=512m`, `BRIDGE_CPUS=1.0` (override in `.env`).
* **Schema:** every table lives in schema **`bridge`**; nothing is created in `public`, and `REVOKE ALL ON SCHEMA bridge FROM PUBLIC` keeps other roles out. `search_path` is set to `bridge` on every connection. Tests check that every table is in `bridge` and that no module imports `sqlite3`.
* **Types:** only standard Postgres types. Money is `NUMERIC(18,4)`; timestamps are `TIMESTAMPTZ`, stored as UTC and shown as IST. No extensions (the allowlist is empty) and no vendor-specific schemas, checked by `tests/test_portability.py`.
* **Instrument cache:** the scrip master is loaded with `COPY` in one transaction; if a refresh fails, the last good cache stays in use.
* **Health:** `db_ok` means `SELECT 1` answered within 3 s **and** `alembic current == head`.
* **Live startup fails fast** if the database is unreachable. **Outages after startup** don't crash the app: `/health` reports `db_ok: false`, `/api` answers 503 (not 401), and background writes (the scrip master refresh) are logged and retried with exponential backoff.
* **No pg tools in the bridge image, none needed on the host.** The host-side ops tools (`scripts/ops.sh`, which runs `dhan_bridge.backup` / `dhan_bridge.dbmove` from the deployed image's code in a hash-locked venv) never open a database connection. Every database step runs `psql` / `pg_dump` / `pg_restore` inside a container:
  * **our database** (the `DATABASE_URL` host is `postgres`): `docker compose exec -T postgres ...`, over the container's local socket, so no password is passed;
  * **any other host**: `docker run --rm -i postgres:<major>-alpine ...`, where `<major>` is the server's own major, so the client is never older than the server. The URL goes in via the container environment, never on a command line.

### Backups and nightly verification

`python -m dhan_bridge.backup` (on a host: `bash ops.sh backup ...`):

| Command | What it does |
|---|---|
| `now` | Consistent snapshot: one psql session opens a REPEATABLE READ, READ ONLY transaction, exports its snapshot (`pg_export_snapshot()`), takes the [fingerprint](#fingerprints) **in that transaction**, and runs `pg_dump -Fc --schema=bridge --snapshot=<id>` while it stays open. Writes `data/backups/bridge-<ts>.dump` and `bridge-<ts>.dump.fingerprint.json` (fingerprint, sha256, source host without password). |
| `verify <file>` | Checks the sha256, restores the dump into a **throwaway** plain `postgres:<major>-alpine` container, fingerprints it and compares with the sidecar. Any difference exits non-zero. |
| `restore <file> [--force]` | Refuses while the bridge container runs unless `--force`. Takes a safety dump (`pre-restore-*.dump`), renames the current schema aside, runs `pg_restore --clean --if-exists --schema=bridge` into a fresh `bridge` schema and compares fingerprints. On success the renamed copy is dropped; on any failure the original schema is put back. |
| `offsite <file>` | Copies the dump, its sidecar and `last-deploy.json` to **every** target in `config/backup-targets.yaml` (rsync over SSH with a host/path/key, or an rclone `remote:path`). Any failed target exits non-zero. |
| `prune [--keep N]` | Keeps the newest N dumps (default `BACKUP_KEEP=7`). |
| `rollback-plan`, `latest` | Used by `remote-deploy.sh`. |

The schema is renamed aside instead of relying on `--clean` alone because a `--schema` restore skips `CREATE SCHEMA`, and tables created after the dump (by a newer release) would otherwise survive the restore.

**Nightly:** `scripts/backup-nightly.sh` runs `now` → `verify` → `offsite` → `prune` (keep 7). Any failure exits non-zero and, if `secrets/discord.env` contains `DISCORD_WEBHOOK_URL=...`, posts the failed step to Discord. Scheduling it (timer or cron) and real off-site targets are part of each host's deployment wiring; `config/backup-targets.yaml` ships as a fully commented example:

```yaml
targets:
  - {name: nas,   type: rsync,  host: backup@nas.example.internal, path: /srv/backups/dhan_bridge/, ssh_key: /home/deploy/.ssh/backup_ed25519}
  - {name: cloud, type: rclone, remote: b2-backups:dhan-bridge/postgres}
```

**Every deploy:** `remote-deploy.sh deploy` runs `backup now --record-deploy` **before pulling** and writes `data/backups/last-deploy.json` = `{previous_tag, target_tag, alembic_revision, backup}`. `rollback` restores that dump when the Alembic revision moved.

### Fingerprints

`dhan_bridge.dbverify` builds one SQL statement from the catalog and the model metadata. It records:

* the Alembic revision and the server major version;
* the exact row count of **every** table in `bridge` (from the catalog, so unknown tables show up too);
* the sum of every `NUMERIC` column (exact, as text) and the min/max of every `TIMESTAMPTZ` column (formatted in UTC by the server). These columns are discovered from `models.py`, so new tables are covered automatically.

The app computes it through SQLAlchemy (`fingerprint(conn)`), the ops tools through `psql` in a container (`\gexec`); a test asserts both give identical JSON. `compare(a, b)` lists every difference; an empty list means identical. `python -m dhan_bridge.dbverify compare a.json b.json` compares two fingerprint files (or backup sidecars).

### Moving to another host

The target can be any PostgreSQL server whose major version is >= ours (another VPS, a managed service). Only `db.env` changes.

```bash
cd /opt/dhan_bridge
# db.env for the new host, readable by group bridge
sudo install -m 0440 -g 10001 /dev/stdin /tmp/target-db.env <<< 'DATABASE_URL=postgresql://user:pass@new-host:5432/db?sslmode=verify-full'
bash db-move.sh --target-secrets /tmp/target-db.env --dry-run   # full copy + verify, then removes the copy
bash db-move.sh --target-secrets /tmp/target-db.env             # the real move (outside market hours)
bash db-move.sh --revert                                        # fallback window: back to the old host
```

1. **Preflight** (nothing changes if it fails): the market-hours guard (`--force-market-hours --reason "..."` to override); target reachable; target major >= source major; target schema `bridge` absent or empty (otherwise `--overwrite-target`); enough local disk for the dump; source and target are not the same database.
2. **Copy:** stop the bridge (not in `--dry-run`, which copies a live snapshot and never stops anything), take a consistent dump, `pg_restore` it into the target.
3. **Verify:** the target's fingerprint must equal the dump's (except `server_major`), and its Alembic revision must be head. On a mismatch, the bad copy is removed, the bridge restarts on the untouched source and the tool exits 1.
4. **Switch:** `db.env` → `db.env.previous`, the target's `db.env` is moved into place atomically (owner, group and mode preserved), the bridge starts and must pass the strict health gate within 120 s. If it doesn't, `db.env.previous` goes back, the bridge restarts on the source and the tool exits 1.

Exit codes: 0 moved (or dry run passed), 1 failed but the source is back and healthy, 2 preflight refused (nothing changed), 3 manual action required. Every run writes `data/migrations-log/<ts>.json` (hosts without passwords, versions, both fingerprints, steps, result). **The source database is only ever read.** The compose `postgres` service keeps running after a move, which is what makes `--revert` possible; keep it for two weeks, then retire it (see the runbook). `--revert` warns that writes made on the new host are **not** copied back.

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
* **Schema.** Everything goes in schema `bridge` (`schema="bridge"` on every table and index); nothing in `public`.
* **Portability.** Only standard types, money as `NUMERIC(18,4)`, `TIMESTAMPTZ` only, no extensions, and no references to vendor-specific schemas (`auth`, `storage`, `cron`, `rdsadmin`, ...) or to `public`. This is checked by `tests/test_portability.py`.
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
apt-get install -y python3 python3-venv rsync       # host-side ops tools (ops.sh) + off-site copy; no pg client needed
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
ALLOWED_ORIGINS=https://app.example.org
EOF
# database password: one random value in both secrets files
pw="$(openssl rand -hex 24)"
install -o 70 -g 70 -m 0400 /dev/stdin secrets/postgres_password <<< "$pw"
install -o deploy -g bridge -m 0440 /dev/stdin secrets/db.env <<< "DATABASE_URL=postgresql://bridge:${pw}@postgres:5432/bridge"
unset pw
install -d -o deploy -g deploy config                # optional: config/backup-targets.yaml (see Database)
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

`/opt/dhan_bridge/secrets/db.env` (see [Database](#database)); the host is the compose service `postgres`:

```
DATABASE_URL=postgresql://bridge:<password>@postgres:5432/bridge
```

`/opt/dhan_bridge/secrets/postgres_password` holds the same password on one line. The postgres image reads it as its own user (uid 70), so this one file is owned `70:70` with mode `0400`. The password only takes effect when the `pgdata` volume is first initialised; to change it later, run `ALTER ROLE bridge PASSWORD '...'` via `docker compose exec postgres psql -U bridge`, then update both files.

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
