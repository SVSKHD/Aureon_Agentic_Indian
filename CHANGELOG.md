# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). CI refuses to build a version that has
no entry here.

## [0.1.0] - 2026-10-13

First release: foundation, packaging and CI/CD. No market feed yet (`feed: "not_started"`).

### Added
- Settings via pydantic-settings (`BRIDGE_MODE`, `UNIVERSE_FILE`, `PAPER_CAPITAL`,
  `ADMIN_KEY_FILE`, ...). Dhan credentials are read only from a secrets file
  (default `/run/secrets/dhan.env`). They are masked in every repr and scrubbed from logs.
- Hot token rotation: secrets file watch (5 s poll, atomic-replace safe) and
  `POST /admin/reload-token` (admin key). No restart or redeploy needed.
- Thin read-only `dhanhq` 2.3.0 wrapper (`DhanContext` + `Funds.get_fund_limits`
  for token verification).
- Scrip master download (once per IST day) cached in SQLite, `NSE:SYMBOL` <->
  `(exchange_segment, security_id)` resolution. Startup fails on unknown universe symbols.
- NIFTY 50 universe file and NSE 2026 holiday calendar.
- IST calendar: trading day, market open (09:15–15:30), deploy guard
  (09:00–15:45 on trading days) with the CLI
  `python -m dhan_bridge.calendar deploy-guard` (exit 0 allowed / 3 blocked).
- Token health check at startup, hourly and after rotation.
  `GET /health` returns `version, mode, token_ok, feed, market_open, db_ok`.
  `python -m dhan_bridge.health probe` is used by the Docker HEALTHCHECK, the smoke test and the deploy gates.
- Versioned SQLite migrations with a backup before applying (`data/backups/`, last 30 kept).
  *(Superseded before release by PostgreSQL + Alembic, see below.)*
- No-trade invariant test (`tests/test_no_trade.py`) and README "Safety" section.
- Hash-locked dependencies (pip-compile `--generate-hashes`), multi-stage
  non-root Dockerfile (single uvicorn worker), prod and staging compose files,
  nginx (TLS, `/ws` upgrade, `/api`, `/health`).
- Jenkins pipeline: lint, tests with coverage gate, no-trade, gitleaks, pip-audit,
  image build, trivy, replay smoke test, push, staging deploy, approved prod deploy
  with market-hours guard, health gate, automatic rollback and Discord notification.
- `docs/RUNBOOK.md`.

### Review fixes before first release
- **Auth:** API keys stored as SHA-256 in SQLite (migration `0002_api_keys`), with
  `python -m dhan_bridge.keys create|list|revoke`. Every `/api/*` route requires
  `Authorization: Bearer <key>`. Public `/health` returns only `{status, version}`;
  the full payload needs a key or a loopback peer. Uvicorn now runs with
  `--no-proxy-headers` so `X-Forwarded-For` cannot fake loopback.
- **WebSocket auth helper (for P2):** the key goes in `Sec-WebSocket-Protocol`
  (`bearer.<key>`) or in the first message. Keys in the query string are refused.
- **nginx:** `limit_req` 10 r/s, burst 20 on `/api` and `/ws`; `/admin` still 404.
- **Backups:** new `dhan_bridge.backup` (`now`, `verify`, `restore` (refuses while
  running unless `--force`), `rollback-plan`). `remote-deploy.sh deploy` takes an
  online backup and writes `data/backups/last-deploy.json` before pulling.
  `deploy/backup-offsite.sh` plus a systemd timer copies the newest backup off-host
  nightly (rsync or rclone). The app holds `data/bridge.lock`, so a second instance
  refuses to start.
- **Rollback:** `remote-deploy.sh rollback` restores the pre-deploy backup when the
  schema version changed, then starts the previous tag. Jenkins auto-rollback uses it.
  Migrations must be expand-only; a test rejects `DROP`/`RENAME` without
  `-- allow-destructive: <reason>`.
- **Build once, promote:** tag builds pull `:<GIT_SHA>` built on main ("never built on
  main" otherwise), retag `:<VERSION>` and push. Smoke test and market-hours guard
  run against the promoted image.
- **Health gate:** `--strict` in live mode requires `token_ok == true` regardless
  of market hours; in replay mode `token_ok` must be `null`.
- **Jenkins:** the pipeline is `agent none`; the prod approval waits without
  holding an executor and the deploy runs in its own stage. Tag builds need the
  Basic Branch Build Strategies plugin (or a manual build).
- **Runtime no-trade guard:** importing `dhan_client` replaces dhanhq's order
  classes and legacy client with stubs that raise `NoTradeViolation`, and the SDK's
  HTTP layer refuses order and account-mutating endpoints.
- **Logging:** the redactor also scrubs tracebacks (`exc_text`) and `stack_info`.
- **Tasks:** token-recheck tasks remove themselves from `state.tasks` when done.

### Storage: PostgreSQL + backup verification + host migration tooling
- **PostgreSQL 16 replaces SQLite**, running in our own compose stack. No SQLite anywhere: a test fails on any `sqlite3` import. Python is the only database client.
  - **Driver and pool:** SQLAlchemy 2 + psycopg 3, a small sync pool (3 + 2 overflow, pre-ping, 10 s connect timeout).
  - **Connection URL:** `sslmode` configurable via `DB_SSLMODE` (default `prefer`). `DATABASE_URL` is read only from `/run/secrets/db.env`, hot-reloaded like `dhan.env`, with the password masked everywhere.
- **Schema `bridge` only:** `search_path` set per connection, nothing created in `public` (tests check every table), `REVOKE ALL ON SCHEMA bridge FROM PUBLIC`.
- **Alembic replaces the custom runner:**
  - **Revisions:** `0001_initial`, `0002_api_keys`.
  - **Tests enforce:** expand-only revisions (`# -- allow-destructive: <reason>` to override), models == revisions (autogenerate finds nothing), and a single linear chain.
- **Portability rules** (tests): standard Postgres types only, money `NUMERIC(18,4)`, `TIMESTAMPTZ` everywhere (UTC stored, IST shown), no extensions (empty allowlist), no vendor-specific schemas or `public`, no hosted-database SDKs in the locks.
- **Instruments cache** loaded with `COPY` in one transaction. A failed download *or* load keeps the stale cache.
- **Resilience:**
  - **Startup:** live startup fails fast without the database. Replay starts anyway and retries in the background.
  - **Outages after startup:** `/health` shows `db_ok: false`, `/api` answers 503, and failed background writes are logged and retried with exponential backoff.
  - **`db_ok`:** `SELECT 1` within 3 s **and** `alembic current == head`.
- **CORS:** `ALLOWED_ORIGINS` (comma-separated exact origins; no paths or wildcards; `*` refused in live mode). CORS middleware on `/api` only, credentials disabled. The WebSocket handshake rejects any other `Origin` with HTTP 403 before the key is looked at.
- **`dhan_bridge.dbverify`:** fingerprints (Alembic revision, server major, exact row count of every `bridge` table, NUMERIC sums and TIMESTAMPTZ min/max discovered from metadata) and `compare()`. One generated SQL statement, used identically by the app (SQLAlchemy) and the ops tools (psql); a test checks both give the same JSON.
- **No pg tools in the bridge image or on the host:** `dhan_bridge.pgtools` runs `psql` / `pg_dump` / `pg_restore` via `docker compose exec -T postgres` for our own database and `docker run --rm postgres:<major>-alpine` (client major = server major) for any other host.
- **Backups** (`python -m dhan_bridge.backup`, on hosts `scripts/ops.sh backup ...`):
  - **`now`:** one psql session: REPEATABLE READ, `pg_export_snapshot()`, the fingerprint in that transaction, then `pg_dump -Fc --schema=bridge --snapshot=<id>`. Dump + `<dump>.fingerprint.json` in `data/backups/`.
  - **`verify`:** restores into a throwaway postgres, compares fingerprints, exits non-zero on any difference.
  - **`restore`:** `pg_restore --clean --if-exists --schema=bridge` into a fresh schema (the old one renamed aside and put back on any failure); refuses while the bridge runs unless `--force`; safety dump first.
  - **`offsite`:** copies to every target in `config/backup-targets.yaml` (rsync host/path/ssh key, rclone `remote:path`); ships as a commented example.
  - **`rollback-plan`**, **`prune`** (keep 7), **`latest`**.
- **`scripts/backup-nightly.sh`:** `now` → `verify` → `offsite` → `prune` (keep 7), optional Discord webhook from a secrets file on failure. Scheduling is left to each host's deployment wiring.
- **Pre-deploy dump and rollback:** the pre-deploy dump plus `last-deploy.json` (Alembic revision, previous tag) and the schema-aware rollback use these commands.
- **`scripts/db-move.sh` / `dhan_bridge.dbmove`** moves the data to any Postgres host with no code change:
  - **Preflight:** market hours (override needs a reason), target reachable, target major >= source, empty target unless `--overwrite-target`, disk.
  - **Copy and verify:** stop the bridge (not in `--dry-run`), consistent dump, `pg_restore` into the target, fingerprint + Alembic head check; any mismatch falls back to the source.
  - **Switch:** `db.env.previous` + atomic `mv` (owner/group/mode kept), 120 s health gate, automatic fallback; `--revert` warns that writes on the target are not copied back.
  - **Log and safety:** a JSON log per run in `data/migrations-log/` (passwords masked). The source is never modified.
- **Compose:**
  - **Prod and staging:** `postgres:16-alpine` pinned by digest, named volume, **no published port**, internal network only, password from `secrets/postgres_password`, `pg_isready` healthcheck, the bridge `depends_on: service_healthy`. Mounted `deploy/postgresql.conf` (`shared_buffers=256MB`, `max_connections=30`). Memory/CPU limits as variables with defaults.
  - **Dev:** the same postgres service, published on loopback.
- **CI:**
  - **Tests:** real PostgreSQL only (throwaway containers; 15, 16 and 17 for the move tests).
  - **Smoke test:** the image plus a throwaway postgres on a private network.
  - **New stage "DB portability":** the db-move and backup-verify suites (16 → 17 move, older/non-empty target refused, market hours, dry run, corrupted restore → fallback, revert; dump/verify/restore/rollback).
