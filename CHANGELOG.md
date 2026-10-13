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
