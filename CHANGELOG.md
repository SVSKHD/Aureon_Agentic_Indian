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
