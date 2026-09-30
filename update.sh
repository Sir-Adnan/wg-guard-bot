#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — update / updater
#
#  What this script does:
#    1) Takes a quick backup of the database (if the services are up)
#    2) Pulls the latest code (git pull --ff-only), or if git is missing just
#       rebuilds the image
#    3) Rebuilds the image and brings the services up with the new code
#    4) Runs the Alembic migrations inside the container
#    5) Prints the resulting version
#
#  Run:  bash update.sh [--branch main] [--dir PATH] [--no-backup] [--yes]
# ---------------------------------------------------------------------------
set -euo pipefail

BRANCH="main"
INSTALL_DIR="$(pwd)"
DO_BACKUP="true"
ASSUME_YES="false"
HEALTH_TIMEOUT="120"

C_RESET=""; C_BOLD=""; C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""
if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'; C_CYAN=$'\033[36m'
fi

say()  { printf '%s\n' "$*"; }
info() { printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
ok()   { printf '%s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s⚠%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s✖%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
step() { printf '\n%s%s▸ %s%s\n' "$C_BOLD" "$C_BLUE" "$*" "$C_RESET"; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

usage() {
    cat <<EOF
${C_BOLD}WG-Guard Bot — update${C_RESET}

${C_BOLD}Usage:${C_RESET}
  bash update.sh [options]

${C_BOLD}Options:${C_RESET}
  ${C_CYAN}--branch NAME${C_RESET}   branch to update from (default: main)
  ${C_CYAN}--dir PATH${C_RESET}      path to the project directory (default: current directory)
  ${C_CYAN}--no-backup${C_RESET}     do not take a backup before updating
  ${C_CYAN}--yes${C_RESET}           confirm everything without asking
  ${C_CYAN}-h, --help${C_RESET}      show this help

${C_DIM}Pulls the latest code (fast-forward only), rebuilds the image, restarts
the stack, runs 'alembic upgrade head' inside the container and prints the
resulting version.${C_RESET}
EOF
}

require_root() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        warn "This script needs root access; re-running it with sudo…"
        exec sudo -E bash "$0" "$@"
    fi
    err "Updating requires root access (and sudo is not installed)."
    exit 1
}

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
        return 0
    fi
    # Compose v1 is end-of-life and lacks --profile / ps --status.
    if command -v docker-compose >/dev/null 2>&1; then
        warn "The old docker-compose (v1) is installed; this script needs Docker Compose v2."
        say "  Install: apt-get update && apt-get install -y docker-compose-plugin"
    fi
    err "Docker Compose not found. Install: apt-get install -y docker-compose-plugin"
    exit 1
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --branch)    BRANCH="${2:-}"; shift ;;
            --branch=*)  BRANCH="${1#*=}" ;;
            --dir)       INSTALL_DIR="${2:-}"; shift ;;
            --dir=*)     INSTALL_DIR="${1#*=}" ;;
            --no-backup) DO_BACKUP="false" ;;
            --yes|-y)    ASSUME_YES="true" ;;
            --help|-h)   usage; exit 0 ;;
            *) err "Unknown option: $1"; say ""; usage; exit 2 ;;
        esac
        shift
    done
}

confirm() {
    local question="$1" default="${2:-n}" answer=""
    if [ "$ASSUME_YES" = "true" ]; then
        [ "$default" = "y" ]
        return $?
    fi
    printf '%s? %s%s %s[%s]%s\n  %s❯%s ' \
        "$C_BOLD" "$question" "$C_RESET" "$C_DIM" "$default" "$C_RESET" "$C_GREEN" "$C_RESET" >&2
    IFS= read -r answer || answer=""
    [ -n "$answer" ] || answer="$default"
    case "$answer" in
        y|Y|yes|YES|بله|ب) return 0 ;;
        *) return 1 ;;
    esac
}

running_services() {
    $COMPOSE ps --services --status running 2>/dev/null || true
}

# ---------------------------------------------------------------------------
#  1) Backup before updating
# ---------------------------------------------------------------------------
take_backup() {
    if [ "$DO_BACKUP" != "true" ]; then
        warn "Backup skipped (--no-backup)."
        return 0
    fi
    step "Taking a backup before the update"
    if ! running_services | grep -q '^db$'; then
        warn "The database is not running; skipping this step."
        return 0
    fi
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    if $COMPOSE exec -T db sh -c \
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' > "./pre-update-${stamp}.sql" 2>/dev/null; then
        if [ -s "./pre-update-${stamp}.sql" ]; then
            ok "Backup saved: ./pre-update-${stamp}.sql"
            return 0
        fi
        rm -f "./pre-update-${stamp}.sql"
        warn "The backup was empty and was deleted."
        return 0
    fi
    rm -f "./pre-update-${stamp}.sql"
    warn "The backup failed; continuing (the bot's automatic backups are still in place)."
    return 0
}

# ---------------------------------------------------------------------------
#  2) Pulling the latest code
# ---------------------------------------------------------------------------
current_version() {
    # First from git, then from pyproject.toml, and finally "unknown"
    if [ -d "$INSTALL_DIR/.git" ] && command -v git >/dev/null 2>&1; then
        local commit
        commit="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || true)"
        [ -n "$commit" ] && { printf '%s' "$commit"; return 0; }
    fi
    if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
        local ver
        ver="$(grep -E '^version[[:space:]]*=' "$INSTALL_DIR/pyproject.toml" | head -n1 | sed -e 's/.*=[[:space:]]*//' -e 's/"//g' -e "s/'//g" || true)"
        [ -n "$ver" ] && { printf '%s' "$ver"; return 0; }
    fi
    printf 'unknown'
    return 0
}

pull_code() {
    step "Fetching the latest code changes"
    if [ ! -d "$INSTALL_DIR/.git" ]; then
        warn "This directory is not a git repository; skipping the code pull and only rebuilding the image."
        dim "  (for automatic code updates, install the project with git clone)"
        return 0
    fi
    if ! command -v git >/dev/null 2>&1; then
        warn "git is not installed; skipping the code pull."
        return 0
    fi
    if ! git -C "$INSTALL_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        warn "The .git directory exists but is not a healthy repository; skipping the code pull."
        return 0
    fi

    local dirty
    dirty="$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null | grep -v '^?? \.env' || true)"
    if [ -n "$dirty" ]; then
        warn "There are local changes in the code:"
        printf '%s\n' "$dirty" | head -n 10 | sed -e 's/^/    /' >&2
        if [ "$ASSUME_YES" != "true" ]; then
            if confirm "Ignore the local changes (stash) and continue the update?" "y"; then
                git -C "$INSTALL_DIR" stash push -u -m "update.sh $(date +%Y%m%d-%H%M%S)" >/dev/null 2>&1 || true
                ok "The changes were set aside for now (git stash)."
            else
                err "Update cancelled; commit or stash the local changes first."
                exit 1
            fi
        else
            warn "--yes mode: the local changes are ignored and stashed."
            git -C "$INSTALL_DIR" stash push -u -m "update.sh $(date +%Y%m%d-%H%M%S)" >/dev/null 2>&1 || true
        fi
    fi

    if git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"; then
        ok "The code was updated to the latest revision of the ${BRANCH} branch."
    else
        err "git pull --ff-only failed (the local branch may be ahead of the remote)."
        say "  Manual fix:"
        dim "    cd $INSTALL_DIR && git fetch origin && git reset --hard origin/$BRANCH"
        say "  Then run this script again."
        exit 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
#  3) Rebuild and restart
# ---------------------------------------------------------------------------
rebuild_and_restart() {
    step "Building the new image and restarting"
    # Stamp the image with the revision we just pulled; verify_running_commit()
    # reads it back from the container afterwards.
    if [ -d "$INSTALL_DIR/.git" ] && command -v git >/dev/null 2>&1; then
        GIT_COMMIT="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || true)"
        export GIT_COMMIT
        if [ -n "$GIT_COMMIT" ]; then
            dim "  Building from revision ${GIT_COMMIT}."
        fi
    fi
    $COMPOSE pull --ignore-pull-failures 2>/dev/null || true
    if ! $COMPOSE build --pull bot; then
        err "Building the image failed."
        return 1
    fi
    ok "The new image was built."
    if ! $COMPOSE up -d --remove-orphans; then
        err "Starting the services failed."
        return 1
    fi
    ok "The services are running with the new version."
    return 0
}

# ---------------------------------------------------------------------------
#  4) Migrations
# ---------------------------------------------------------------------------
run_migrations() {
    step "Running the database migrations"
    local tries=1
    while [ "$tries" -le 20 ]; do
        if $COMPOSE exec -T bot alembic upgrade head; then
            ok "The database is at the latest migration revision."
            return 0
        fi
        dim "  Attempt ${tries}/20 — the bot is not ready yet, waiting 3 seconds…"
        sleep 3
        tries=$(( tries + 1 ))
    done
    err "Running the migrations failed."
    dim "  Bot logs:  $COMPOSE logs --tail=40 bot"
    return 1
}

# ---------------------------------------------------------------------------
#  5) Health check and version output
# ---------------------------------------------------------------------------
panel_port() {
    local port=""
    port="$(grep -E '^[[:space:]]*PANEL_PORT=' "$INSTALL_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2 | tr -d '[:space:]' || true)"
    [ -n "$port" ] || port="8080"
    printf '%s' "$port"
}

wait_health() {
    local health_port="$1" waited=0 url=""
    url="http://127.0.0.1:${health_port}/healthz"
    step "Checking service health (up to ${HEALTH_TIMEOUT} seconds)"
    while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
        if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
            ok "The panel is healthy: $url"
            return 0
        fi
        if ! command -v curl >/dev/null 2>&1; then
            if $COMPOSE ps --status running 2>/dev/null | grep -q bot; then
                sleep 5
                return 0
            fi
        fi
        sleep 3
        waited=$(( waited + 3 ))
    done
    warn "The panel did not respond within ${HEALTH_TIMEOUT} seconds; check the logs:"
    say "    $COMPOSE logs --tail=40 bot"
    return 1
}

app_version() {
    # We ask the container itself for the version; if that fails, we use the local code
    local ver=""
    ver="$($COMPOSE exec -T bot python -c \
        'import importlib.metadata as m; print(m.version("wg-guard-bot"))' 2>/dev/null | tr -d '\r\n' || true)"
    if [ -z "$ver" ]; then
        ver="$(current_version)"
    fi
    printf '%s' "$ver"
    return 0
}

# ---------------------------------------------------------------------------
#  6) Prove that the container runs the code we just pulled
# ---------------------------------------------------------------------------
running_commit() {
    # The revision the *image* was built from (Dockerfile ARG GIT_COMMIT).  An
    # empty answer means the image carries no stamp — itself worth reporting.
    local commit=""
    commit="$($COMPOSE exec -T bot python -c 'import app; print(app.__commit__)' 2>/dev/null | tr -d '\r\n' || true)"
    [ -n "$commit" ] || commit="unknown"
    printf '%s' "$commit"
    return 0
}

same_commit() {
    # True when one revision is a prefix of the other (short vs. long sha).
    [ -n "$1" ] && [ -n "$2" ] || return 1
    case "$2" in "$1"*) return 0 ;; esac
    case "$1" in "$2"*) return 0 ;; esac
    return 1
}

verify_running_commit() {
    # An update that silently keeps running the old image is the one failure that
    # looks like success, so it is checked rather than assumed: a fix that "did
    # not work" is usually a fix that never reached the container.
    local checkout=""
    if ! command -v git >/dev/null 2>&1 || [ ! -d "$INSTALL_DIR/.git" ]; then
        return 0
    fi
    checkout="$(git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || true)"
    [ -n "$checkout" ] || return 0

    RUNNING_COMMIT="$(running_commit)"
    if [ "$RUNNING_COMMIT" = "unknown" ]; then
        warn "The running image does not report the revision it was built from."
        dim "  Rebuild it with:  cd $INSTALL_DIR && GIT_COMMIT=$checkout $COMPOSE build bot"
        return 0
    fi
    if same_commit "$checkout" "$RUNNING_COMMIT"; then
        ok "The container is running revision ${RUNNING_COMMIT}."
        return 0
    fi

    err "The container is still running revision ${RUNNING_COMMIT}, but the checkout is at ${checkout}."
    say "  The image was not rebuilt from the current code, so nothing that was pulled is live."
    say "  Rebuild and recreate it by hand:"
    dim "    cd $INSTALL_DIR"
    dim "    git log --oneline -1"
    dim "    GIT_COMMIT=$checkout $COMPOSE build --pull --no-cache bot"
    dim "    GIT_COMMIT=$checkout $COMPOSE up -d --force-recreate bot"
    dim "    curl -s http://127.0.0.1:$(panel_port)/healthz"
    return 1
}

# ===========================================================================
#  main
# ===========================================================================
main() {
    parse_args "$@"

    # Check for root first, so that no duplicate output is printed when sudo is needed
    require_root "$@"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — update${C_RESET}"

    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
        err "docker-compose.yml was not found in \"$INSTALL_DIR\"."
        say "  Use --dir to give the correct path, for example: bash update.sh --dir /root/wg-guard-bot"
        exit 1
    fi
    cd "$INSTALL_DIR"
    if [ ! -f .env ]; then
        err ".env was not found; it looks like you have not installed yet."
        say "  Install first: bash install.sh"
        exit 1
    fi

    detect_compose
    ok "Docker is ready (${COMPOSE})."

    VERSION_BEFORE="$(current_version)"
    dim "  Current version: ${VERSION_BEFORE}"

    take_backup
    pull_code

    if ! rebuild_and_restart; then
        say ""
        err "The update did not finish."
        $COMPOSE ps 2>&1 | tail -n 15 || true
        say ""
        $COMPOSE logs --tail=40 bot 2>&1 || true
        say ""
        dim "To roll back, run the previous image version with an explicit tag in docker-compose.override.yml."
        exit 1
    fi

    if ! run_migrations; then
        $COMPOSE logs --tail=40 bot 2>&1 || true
        exit 1
    fi

    wait_health "$(panel_port)" || true

    # The last word on whether the update is real: what the container reports.
    RUNNING_COMMIT="unknown"
    if ! verify_running_commit; then
        say ""
        err "The update did not take effect: the old image is still running."
        exit 1
    fi

    VERSION_AFTER="$(app_version)"
    [ -n "$VERSION_AFTER" ] || VERSION_AFTER="unknown"

    say ""
    printf '%s%s%s\n' "$C_GREEN" "════════════════════════════════════════════════════" "$C_RESET"
    printf '%s  ✅  The update completed successfully.%s\n' "$C_GREEN" "$C_RESET"
    printf '%s%s%s\n' "$C_GREEN" "════════════════════════════════════════════════════" "$C_RESET"
    say ""
    printf '  Previous version : %s\n' "$VERSION_BEFORE"
    printf '  New version      : %s%s%s\n' "$C_BOLD" "$VERSION_AFTER" "$C_RESET"
    printf '  Source revision  : %s\n' "$(current_version)"
    printf '  Running revision : %s%s%s\n' "$C_BOLD" "$RUNNING_COMMIT" "$C_RESET"
    printf '  Branch           : %s\n' "$BRANCH"
    say ""
    info "Useful commands:"
    printf '     %s logs -f bot%s      → view live logs\n' "$COMPOSE" "$C_RESET"
    printf '     %s ps%s               → service status\n' "$COMPOSE" "$C_RESET"
    say ""
    return 0
}

main "$@"
