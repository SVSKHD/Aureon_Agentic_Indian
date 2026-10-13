# syntax=docker/dockerfile:1
# python:3.11-slim, pinned by multi-arch index digest. Bump deliberately (and re-run the trivy gate).
ARG PYTHON_IMAGE=python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce

# ---------------------------------------------------------------- build stage
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
# --require-hashes: every artifact must match the lock file, or the build fails.
RUN /opt/venv/bin/pip install --require-hashes --no-deps -r /tmp/requirements.txt \
 && /opt/venv/bin/pip uninstall --yes setuptools pip

# ---------------------------------------------------------------- runtime stage
FROM ${PYTHON_IMAGE} AS runtime
ARG VERSION=dev
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="dhan_bridge" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data \
    UNIVERSE_FILE=/app/universe/nifty50.txt \
    HOLIDAYS_DIR=/app/holidays \
    SECRETS_FILE=/run/secrets/dhan.env \
    ADMIN_KEY_FILE=/run/secrets/admin.key

# The runtime needs no packaging tools: drop them (and their vendored, CVE-prone deps).
RUN python -m pip uninstall --yes --no-input pip setuptools wheel \
 && groupadd --system --gid 10001 bridge \
 && useradd --system --uid 10001 --gid bridge --home-dir /app --shell /usr/sbin/nologin bridge

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY dhan_bridge/ /app/dhan_bridge/
COPY universe/ /app/universe/
COPY holidays/ /app/holidays/
RUN mkdir -p /app/data/backups && chown -R bridge:bridge /app/data

USER bridge
EXPOSE 8000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-m", "dhan_bridge.health", "probe"]

# Exactly ONE worker: the Dhan feed must exist once per deployment (the app also
# refuses to start if another instance holds data/bridge.lock).
# --no-proxy-headers: request.client must be the real TCP peer. /health trusts
# loopback peers, so X-Forwarded-For must never be able to fake 127.0.0.1.
CMD ["uvicorn", "--factory", "dhan_bridge.api:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1", \
     "--no-proxy-headers", "--no-server-header"]
