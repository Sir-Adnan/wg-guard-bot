# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
#  WG-Guard Bot — production image
#
#  Stage 1 (builder): compiles/installs the pinned runtime dependencies from
#                     requirements.txt into a self-contained venv at /opt/venv.
#  Stage 2 (runtime): python:3.12-slim + postgresql-client (pg_dump for backups),
#                     curl (healthcheck), tzdata and ca-certificates.  Runs as a
#                     non-root user and never ships a compiler.
#
#  Build:  docker build -t wg-guard-bot .
#  Run:    docker compose up -d --build
# ---------------------------------------------------------------------------

# ===========================================================================
#  1) builder — resolve & install requirements into /opt/venv
# ===========================================================================
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Build-only toolchain.  These packages stay in this stage and are never
# copied into the final image.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
        libssl-dev; \
    rm -rf /var/lib/apt/lists/*

# Create the virtualenv the runtime stage will receive.
RUN python -m venv /opt/venv

# Only the requirements file is needed to warm the dependency layer, so edits
# to the application source do not invalidate the (slow) pip install.
COPY requirements.txt /tmp/requirements.txt

# --no-cache-dir keeps the layer small; PYTHONDONTWRITEBYTECODE=1 above keeps
# the venv free of __pycache__ noise.
RUN set -eux; \
    /opt/venv/bin/python -m pip install --upgrade pip setuptools wheel; \
    /opt/venv/bin/python -m pip install --no-cache-dir -r /tmp/requirements.txt; \
    rm -f /tmp/requirements.txt

# ===========================================================================
#  2) runtime — slim image, non-root, pg_dump available for backups
# ===========================================================================
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="WG-Guard Bot" \
      org.opencontainers.image.description="Telegram VPN shop bot for WG-Guard (AmneziaWG) panels — sales, card-to-card receipts, admin panel" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/USER/REPO"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Tehran

# Runtime system packages only:
#   postgresql-client -> pg_dump / pg_isready (backups + entrypoint wait loop)
#   curl              -> container HEALTHCHECK against /healthz
#   tzdata            -> Asia/Tehran for reports and Jalali dates
#   ca-certificates   -> TLS to api.telegram.org and upstream panels
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        postgresql-client \
        curl \
        tzdata \
        ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Unprivileged runtime user (uid/gid 1000) — matches the host user on a stock
# Ubuntu/Debian VPS so bind-mounted files stay writable.
RUN set -eux; \
    groupadd --gid 1000 appuser; \
    useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash appuser

WORKDIR /app

# Virtualenv built in stage 1.
COPY --from=builder --chown=1000:1000 /opt/venv /opt/venv

# Application source.  Keep this list explicit so build context changes stay
# predictable (see .dockerignore for what is excluded).
COPY --chown=1000:1000 alembic.ini ./alembic.ini
COPY --chown=1000:1000 alembic ./alembic
COPY --chown=1000:1000 scripts ./scripts
COPY --chown=1000:1000 app ./app
COPY --chown=1000:1000 pyproject.toml ./pyproject.toml

# Entrypoint must be executable inside the image regardless of the mode git
# checked it out with on Windows.
RUN set -eux; \
    chmod +x /app/scripts/docker-entrypoint.sh; \
    mkdir -p /app/backups /app/logs; \
    chown -R appuser:appuser /app

# SQL dumps produced by the in-app backup job (wgguard-*.sql) live here.
VOLUME ["/app/backups"]

USER appuser

# Panel / webhook HTTP port.  Override with PANEL_PORT.
EXPOSE 8080

# `docker run ... <cmd>` still goes through the entrypoint (DB wait + migrations)
# so one-off commands such as `docker compose run --rm bot bash` behave.
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]

# No CMD on purpose.  The entrypoint ends with `exec uvicorn app.main:app --host
# "$PANEL_HOST" --port "$PANEL_PORT" ...`, which gives two things a shell-form
# CMD cannot: ${PANEL_HOST}/${PANEL_PORT} are honoured, and `exec` makes uvicorn
# PID 1 so SIGTERM reaches it directly (clean shutdown of the bot + scheduler).
# Passing an explicit command (`docker run image bash`) still overrides it.

# Container-level health probe (docker compose also defines its own).
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PANEL_PORT:-8080}/healthz" || exit 1
