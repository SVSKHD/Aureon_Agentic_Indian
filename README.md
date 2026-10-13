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
  api.py           FastAPI app (create_app factory)
  migrations/      versioned *.sql migrations + runner (with backups)
tests/             pytest suite (no network)
deploy/            compose files (prod, staging), nginx.conf, remote-deploy.sh
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
curl localhost:8000/health
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

Paper-trading code must use its own names and routes, for example `submit_paper_order` and `/api/paper/...`. These don't match the banned patterns. **Never add an exclusion to the no-trade test.** If it fires, the code is wrong.

Secrets are handled with the same care:

* Dhan credentials are read only from the secrets file. They never come from environment variables, compose files or Jenkins.
* Every `repr` and `str` of the secrets is masked. A logging filter also scrubs the raw values from every log record. Logs show a short SHA-256 fingerprint so you can confirm that a rotation took effect.
* Gitleaks scans the repository in CI.

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

### What Jenkins does

`main` → lint → test → safety gates → build → trivy → smoke → push `:<sha>` → staging.

Tag `vX.Y.Z` → same pipeline → push `:<version>` and `:<sha>`. A member of `admins` must approve, then the market-hours guard runs, then the deploy, then the health gate. If the health gate fails, Jenkins rolls back automatically. See `Jenkinsfile`.

### Manual rollback

```bash
ssh deploy@prod
cd /opt/dhan_bridge
docker inspect --format '{{.Config.Image}}' dhan_bridge     # what is running now
bash remote-deploy.sh deploy 0.0.9   # or: sed -i 's/^BRIDGE_TAG=.*/BRIDGE_TAG=0.0.9/' .env && docker compose up -d
bash remote-deploy.sh health 120 --strict
```

Jenkins copies `remote-deploy.sh` to `/opt/dhan_bridge/` on every deploy. If the newer version applied a migration the old one can't read, restore the pre-migration backup. See [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Releasing

1. Bump `__version__` in `dhan_bridge/config.py` and add a `## [x.y.z]` section to `CHANGELOG.md`. CI fails without it.
2. Merge to `main`, then tag `vx.y.z` (it must equal `__version__`) and push the tag.
3. Approve the prod deploy in Jenkins, outside 09:00–15:45 IST on trading days.

## Dependencies

`requirements.in` / `requirements-dev.in` are the sources. The lock files are generated with `make lock`, which runs `pip-compile --generate-hashes`. Installs always use `--require-hashes`.
