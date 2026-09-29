#!/bin/sh
# ---------------------------------------------------------------------------
#  WG-Guard Bot — container entrypoint
#
#  POSIX sh (works with dash as /bin/sh).  Responsibilities, in order:
#    1. friendly banner
#    2. wait for PostgreSQL (pg_isready) using DATABASE_URL or POSTGRES_*
#    3. apply Alembic migrations unless RUN_MIGRATIONS=false
#    4. exec the requested command, or uvicorn by default
#
#  Environment knobs:
#    RUN_MIGRATIONS=false     skip `alembic upgrade head`
#    DB_WAIT_TRIES=60         number of pg_isready attempts (1..)
#    DB_WAIT_INTERVAL=2       seconds between attempts
#    DB_WAIT_SKIP=true        do not wait for the database at all
# ---------------------------------------------------------------------------
set -eu

log() {
    # Simple, timestamp-free logger — the app configures structured logging.
    printf '[entrypoint] %s\n' "$*"
}

warn() {
    printf '[entrypoint] WARN: %s\n' "$*" >&2
}

die() {
    printf '[entrypoint] ERROR: %s\n' "$*" >&2
    exit 1
}

# Percent-decode the handful of characters that realistically show up in a
# database URL (passwords with @, :, /, #, ? and % itself).  Takes one argument,
# prints the decoded value — always exits 0 so it is safe under `set -e`.
_decode() {
    printf '%s' "$1" | sed -e 's|%40|@|g' -e 's|%3A|:|g' -e 's|%2F|/|g' \
                           -e 's|%23|#|g' -e 's|%3F|?|g' -e 's|%25|%|g' \
                           -e 's|%2B|+|g' -e 's|%20| |g' | head -n1
    return 0
}

# ---------------------------------------------------------------------------
# 1) banner
# ---------------------------------------------------------------------------
log "=============================================================="
log " WG-Guard Bot — starting container"
log " python      : $(python -V 2>&1 || echo 'not found')"
log " env         : ${ENV:-production}"
log " bot mode    : ${BOT_MODE:-polling}"
log " panel bind  : ${PANEL_HOST:-0.0.0.0}:${PANEL_PORT:-8080}"
log " pg_dump     : $(command -v pg_dump || echo 'MISSING')"
log "=============================================================="

# ---------------------------------------------------------------------------
# 2) wait for PostgreSQL
# ---------------------------------------------------------------------------
db_host=""
db_port=""
db_user=""
db_name=""

# -- derive connection details ------------------------------------------------
# `_dsn` is the part after the scheme:  user:pw@host:5432/db?x=1
_dsn=""
if [ -n "${DATABASE_URL:-}" ]; then
    _dsn=$(printf '%s' "$DATABASE_URL" | sed -e 's|^[^:/?#]*://||' -e 's|[?].*$||' | head -n1)
fi

if [ -n "$_dsn" ]; then
    log "using DATABASE_URL for the database connection"
    # credentials are everything between the first ':' and the LAST '@'
    if printf '%s' "$_dsn" | grep -q '@'; then
        _creds=${_dsn%@*}                                    # user:password
        _hostpart=${_dsn##*@}                                # host:port/db
        # NB: `var=$(func ...)` — `var=func ...` would run `var` as a command.
        db_user=$(_decode "$(printf '%s' "${_creds%%:*}" | sed -e 's|^/*||' | head -n1)")
        _password=$(_decode "$(printf '%s' "$_creds" | sed -e 's|^[^:]*:||' | head -n1)")
        if [ -n "$_password" ]; then
            PGPASSWORD="$_password"
            export PGPASSWORD
        fi
    else
        _hostpart=$_dsn
    fi
    # host from  host[:port]/db
    _netpart=${_hostpart%%/*}
    db_name=$(_decode "$(printf '%s' "$_hostpart" | sed -e 's|^[^/]*/||' | head -n1)")
    case "$_netpart" in
        *:*) db_host=${_netpart%:*}; db_port=${_netpart##*:} ;;
        *)   db_host=$_netpart; db_port=${POSTGRES_PORT:-5432} ;;
    esac
    [ -n "$db_name" ] || db_name=${POSTGRES_DB:-wgguard}
else
    if [ -n "${DATABASE_URL:-}" ]; then
        warn "DATABASE_URL is set but empty/unparsable — falling back to POSTGRES_*"
    fi
    db_host=${POSTGRES_HOST:-db}
    db_port=${POSTGRES_PORT:-5432}
    db_user=${POSTGRES_USER:-wgguard}
    db_name=${POSTGRES_DB:-wgguard}
fi

# -- fall back to POSTGRES_* for anything the DSN did not provide -------------
[ -n "$db_user" ] || db_user=${POSTGRES_USER:-wgguard}
[ -n "$db_host" ] || db_host=${POSTGRES_HOST:-db}
[ -n "$db_port" ] || db_port=${POSTGRES_PORT:-5432}
[ -n "$db_name" ] || db_name=${POSTGRES_DB:-wgguard}

if [ "${DB_WAIT_SKIP:-false}" = "true" ]; then
    warn "DB_WAIT_SKIP=true — skipping the PostgreSQL readiness check"
elif ! command -v pg_isready >/dev/null 2>&1; then
    warn "pg_isready not found — skipping the PostgreSQL readiness check"
else
    tries=${DB_WAIT_TRIES:-60}
    interval=${DB_WAIT_INTERVAL:-2}
    log "waiting for PostgreSQL at ${db_host}:${db_port} (user=${db_user}, db=${db_name})..."

    i=1
    ready=false
    while [ "$i" -le "$tries" ]; do
        if pg_isready --quiet --host="$db_host" --port="$db_port" --username="$db_user" --dbname="$db_name" 2>/dev/null; then
            ready=true
            break
        fi
        # `pg_isready` with a dbname still reports "accepting connections" for a
        # missing database in some server versions — the no-dbname probe is the
        # authoritative one, so use it as a fallback.
        if pg_isready --quiet --host="$db_host" --port="$db_port" --username="$db_user" 2>/dev/null; then
            ready=true
            break
        fi
        if [ $((i % 5)) -eq 0 ]; then
            log "  still waiting for the database... (${i}/${tries})"
        fi
        i=$((i + 1))
        sleep "$interval"
    done

    if [ "$ready" != "true" ]; then
        die "PostgreSQL at ${db_host}:${db_port} did not become ready after $((tries * interval))s.
       * are the db service and its credentials (POSTGRES_*) correct?
       * if the database is remote, check the network/firewall and DATABASE_URL.
       * to start anyway: DB_WAIT_SKIP=true, or raise DB_WAIT_TRIES."
    fi
    log "PostgreSQL is accepting connections ✅"
fi

# ---------------------------------------------------------------------------
# 3) database migrations
# ---------------------------------------------------------------------------
if [ "${RUN_MIGRATIONS:-true}" = "false" ]; then
    warn "RUN_MIGRATIONS=false — skipping 'alembic upgrade head'"
else
    if [ -f /app/alembic.ini ]; then
        log "applying database migrations (alembic upgrade head)..."
        alembic upgrade head
        log "migrations are up to date ✅"
    else
        warn "alembic.ini not found in /app — skipping migrations"
    fi
fi

# ---------------------------------------------------------------------------
# 4) hand over to the requested command (or the default uvicorn server)
# ---------------------------------------------------------------------------
if [ "$#" -gt 0 ]; then
    first=$1
    case "$first" in
        -*)
            # A bare flag (e.g. `--workers 2`) is not a command: treat the whole
            # argv as uvicorn arguments.
            log "no command given (flags only) — starting uvicorn $*"
            exec uvicorn app.main:app \
                --host "${PANEL_HOST:-0.0.0.0}" \
                --port "${PANEL_PORT:-8080}" \
                --proxy-headers --forwarded-allow-ips='*' \
                "$@"
            ;;
        *)
            if command -v "$first" >/dev/null 2>&1; then
                log "executing: $*"
                exec "$@"
            fi
            warn "'$first' is not on PATH — falling back to the default uvicorn server"
            ;;
    esac
fi

log "starting uvicorn on ${PANEL_HOST:-0.0.0.0}:${PANEL_PORT:-8080}"
exec uvicorn app.main:app \
    --host "${PANEL_HOST:-0.0.0.0}" \
    --port "${PANEL_PORT:-8080}" \
    --proxy-headers --forwarded-allow-ips='*'
