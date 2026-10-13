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

### Storage: PostgreSQL on Supabase + host migration tooling
- **PostgreSQL replaces SQLite** (no SQLite anywhere; a test enforces it).
  - **Driver and pool:** SQLAlchemy 2 + psycopg 3, a small sync pool (3 + 2 overflow, pre-ping, 10 s connect timeout).
  - **Connection URL:** `sslmode` defaults to `require`. `DATABASE_URL` is read only from `/run/secrets/db.env`, hot-reloaded like `dhan.env`, with the password masked everywhere.
  - **Supabase:** session pooler (IPv4) or direct URL; never the transaction pooler. No Supabase SDK, Auth, PostgREST, Storage or Edge Functions: Python is the only database client.
- **Schema `bridge` only**, `search_path` set per connection, **RLS enabled on every table with no policies**. The app connects as the owner. The README warns never to expose `bridge` in Supabase's Data API.
- **Alembic replaces the custom runner:**
  - **Revisions:** `0001_initial`, `0002_api_keys`, `0003_heartbeat`, `0004_enable_rls`.
  - **Tests enforce:** expand-only revisions (`# -- allow-destructive: <reason>` to override), models == revisions (autogenerate finds nothing), and a single linear chain.
- **Portability rules** (tests): standard Postgres types only, money `NUMERIC(18,4)`, `TIMESTAMPTZ` everywhere (UTC stored, IST shown), no extensions (empty allowlist), and no references to Supabase schemas or `public`.
- **Instruments cache** replaced in one transaction with a bulk `COPY`. A failed download *or* load keeps the stale cache.
- **Resilience:**
  - **Startup:** live startup fails fast without the database. Replay starts anyway and retries in the background.
  - **Outages after startup:** `/health` shows `db_ok: false`, `/api` answers 503, and background writes retry with exponential backoff.
  - **`db_ok`:** `SELECT 1` within 3 s **and** `alembic current == head`.
- **Heartbeat:** a daily single-row write to `bridge.heartbeat` keeps a free Supabase project from pausing.
- **`dhan_bridge.dbverify`:** fingerprints (Alembic revision, server major, exact row counts, NUMERIC sums, TIMESTAMPTZ min/max, discovered from metadata) and `compare()`.
- **Backups** (host side, `deploy/ops.sh backup ...`; pg tools run in `postgres:<major>-alpine` containers, never in the bridge image):
  - **`now`:** a REPEATABLE READ snapshot, `pg_export_snapshot()`, the fingerprint in the same transaction, then `pg_dump -Fc --schema=bridge --snapshot=<id>`.
  - **`verify`:** restores into a throwaway plain Postgres and compares fingerprints.
  - **`restore`:** refuses while the bridge runs, takes a safety dump, swaps schemas atomically and verifies.
  - **`rollback-plan`** and **`prune`**.
- **Nightly job** (`deploy/backup-nightly.sh` + systemd timer): `now`, `verify`, off-host copy (rsync/rclone), prune to 30, optional Discord alert. Works by hand too.
- **Pre-deploy dump and rollback:** the pre-deploy dump plus `last-deploy.json` (Alembic revision, previous tag) and the schema-aware rollback now use these commands.
- **`deploy/db-move.sh` / `dhan_bridge.dbmove`** moves the data to any other Postgres host:
  - **Preflight:** market hours, target version, empty target, disk.
  - **Copy and verify:** a consistent copy, fingerprint-verified on the target.
  - **Switch:** an atomic `db.env` switch with a health gate and automatic fallback; `--dry-run` and `--revert`.
  - **Log and safety:** a JSON log per run in `data/migrations-log/`. The source is never modified.
- **Compose:**
  - **Prod and staging:** no database service; `db.env` in the secrets dir.
  - **`deploy/docker-compose.dev.yml`:** local PostgreSQL 16 with the same schema.
- **CI:**
  - **Tests:** run against real throwaway PostgreSQL containers.
  - **Smoke test:** runs the image against a throwaway Postgres on a private network.
  - **New stage "DB portability":** a real 16 → 17 move between throwaway servers.

