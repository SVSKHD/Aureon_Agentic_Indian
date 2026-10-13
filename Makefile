PY ?= .venv/bin/python
PIP_COMPILE = $(PY) -m piptools compile --generate-hashes --allow-unsafe --strip-extras --quiet

.PHONY: venv lock lint test check run dev-db

venv:
	python3.11 -m venv .venv
	$(PY) -m pip install --require-hashes -r requirements.txt -r requirements-dev.txt

lock:
	$(PIP_COMPILE) --output-file requirements.txt requirements.in
	$(PIP_COMPILE) --output-file requirements-dev.txt requirements-dev.in
	$(PIP_COMPILE) --output-file requirements-ops.txt requirements-ops.in

lint:
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .
	$(PY) -m mypy
	$(PY) -m mypy --strict --follow-imports=silent dhan_bridge/calendar.py dhan_bridge/config.py

test:
	$(PY) -m pytest -q

check: lint test

# Local PostgreSQL 16: the production postgres service, published on loopback (deploy/docker-compose.dev.yml)
dev-db:
	docker compose -f deploy/docker-compose.dev.yml up -d --wait postgres

run:
	BRIDGE_MODE=replay SECRETS_FILE=./secrets/dhan.env ADMIN_KEY_FILE=./secrets/admin.key \
		DB_SECRETS_FILE=./secrets/db.env DB_SSLMODE=disable ALLOWED_ORIGINS=http://localhost:5173 \
		$(PY) -m uvicorn --factory dhan_bridge.api:create_app --workers 1 --port 8000
