#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — removal / uninstaller
#
#  Default: only the containers and the service network are removed.
#           "The database, Redis and the backups are left untouched."
#
#  With --purge: the data volumes (pgdata, redisdata, backups) and the backups/
#                directory are removed as well — only after a typed "yes".
#
#  Run:  bash uninstall.sh [--purge] [--dir PATH] [--yes] [--keep-images]
# ---------------------------------------------------------------------------
set -euo pipefail

INSTALL_DIR="$(pwd)"
PURGE="false"
ASSUME_YES="false"
KEEP_IMAGES="false"

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
${C_BOLD}WG-Guard Bot — uninstall${C_RESET}

${C_BOLD}Usage:${C_RESET}
  bash uninstall.sh [options]

${C_BOLD}Options:${C_RESET}
  ${C_CYAN}--purge${C_RESET}         delete all data (database, Redis and backups) — with a typed confirmation
  ${C_CYAN}--keep-images${C_RESET}   keep the built image
  ${C_CYAN}--dir PATH${C_RESET}      path to the project directory (default: current directory)
  ${C_CYAN}--yes${C_RESET}           skip the confirmation questions (the typed --purge confirmation is not skipped)
  ${C_CYAN}-h, --help${C_RESET}      show this help

${C_BOLD}How the two modes differ:${C_RESET}
  without --purge : the containers go away, ${C_BOLD}the data stays${C_RESET} (the next install brings the same data back)
  with --purge    : everything goes away; ${C_BOLD}there is no way back${C_RESET}

${C_DIM}Stops and removes the containers.  Volumes and data are KEPT by default;
--purge deletes them after an explicit typed confirmation.${C_RESET}
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
    err "Removing the services requires root access (and sudo is not installed)."
    exit 1
}

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    else
        COMPOSE=""
    fi
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --purge)        PURGE="true" ;;
            --keep-images)  KEEP_IMAGES="true" ;;
            --yes|-y)       ASSUME_YES="true" ;;
            --dir)          INSTALL_DIR="${2:-}"; shift ;;
            --dir=*)        INSTALL_DIR="${1#*=}" ;;
            --help|-h)      usage; exit 0 ;;
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

# ===========================================================================
#  main
# ===========================================================================
main() {
    parse_args "$@"

    # Check for root first, so that no duplicate output is printed when sudo is needed
    require_root "$@"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — uninstall${C_RESET}"

    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
        err "docker-compose.yml was not found in \"$INSTALL_DIR\"."
        say "  Use --dir to give the correct path, for example: bash uninstall.sh --dir /root/wg-guard-bot"
        exit 1
    fi
    cd "$INSTALL_DIR"

    detect_compose
    if [ -z "$COMPOSE" ]; then
        err "Docker Compose not found; the services cannot be managed."
        say "  If you removed Docker by hand, the containers are already gone too."
        exit 1
    fi

    # -----------------------------------------------------------------------
    #  Default mode: containers only, the data is preserved
    # -----------------------------------------------------------------------
    if [ "$PURGE" != "true" ]; then
        say ""
        info "Default mode: the containers are removed but the data stays. 💾"
        dim "  That means the database, Redis and the backups are untouched, and running"
        dim "  install.sh again brings the same information back."
        say ""

        if ! confirm "Stop the services and remove the containers?" "y"; then
            info "Cancelled; nothing was changed."
            exit 0
        fi

        step "Stopping the services"
        $COMPOSE down --remove-orphans || true
        ok "The containers and the network were removed."

        if [ "$KEEP_IMAGES" != "true" ]; then
            step "Removing the built bot image (the build cache is left untouched)"
            docker image rm -f "${IMAGE_NAME:-wgguard-bot}:${IMAGE_TAG:-latest}" >/dev/null 2>&1 || true
            ok "The image was removed."
        fi

        say ""
        printf '%s%s%s\n' "$C_YELLOW" "════════════════════════════════════════════════════" "$C_RESET"
        printf '%s  💾  Your data was preserved — nothing was deleted.%s\n' "$C_YELLOW" "$C_RESET"
        printf '%s%s%s\n' "$C_YELLOW" "════════════════════════════════════════════════════" "$C_RESET"
        say ""
        info "These are still on the server:"
        printf '   • Postgres database  : volume "%spgdata%s"\n' "$C_CYAN" "$C_RESET"
        printf '   • Redis data         : volume "%sredisdata%s"\n' "$C_CYAN" "$C_RESET"
        printf '   • Backups            : volume "%sbackups%s"\n' "$C_CYAN" "$C_RESET"
        printf '   • Configuration file : %s (contains the secrets)\n' "${INSTALL_DIR}/.env"
        say ""
        info "To bring the services back:"
        dim "    cd $INSTALL_DIR && $COMPOSE up -d"
        say ""
        warn "If you really want everything deleted: bash uninstall.sh --purge"
        say ""
        return 0
    fi

    # -----------------------------------------------------------------------
    #  --purge mode: complete removal with a typed confirmation
    # -----------------------------------------------------------------------
    say ""
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    printf '%s%s  🚨  Warning: complete removal mode (--purge) is active!%s\n' "$C_BOLD" "$C_RED" "$C_RESET"
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    say ""
    say "  If you continue, these will be deleted ${C_BOLD}forever${C_RESET}:"
    printf '   %s✖%s all users, orders, receipts and database settings\n' "$C_RED" "$C_RESET"
    printf '   %s✖%s the conversation state and the Redis cache\n' "$C_RED" "$C_RESET"
    printf '   %s✖%s all backup files (wgguard-*.sql)\n' "$C_RED" "$C_RESET"
    say ""
    warn "Suggestion: take a backup copy on your own computer before continuing."
    dim "    $COMPOSE exec -T db pg_dump -U \"\$POSTGRES_USER\" \"\$POSTGRES_DB\" > backup.sql"
    say ""

    if [ "$ASSUME_YES" != "true" ]; then
        printf '%sTo confirm, type the word yes (anything else = cancel):%s\n  %s❯%s ' \
            "$C_BOLD" "$C_RESET" "$C_RED" "$C_RESET" >&2
        typed=""
        IFS= read -r typed || typed=""
        if [ "$typed" != "yes" ]; then
            info "Cancelled; no data was deleted. ✅"
            exit 0
        fi
    else
        warn "--yes mode: skipping the typed confirmation (you asked for --purge yourself)."
    fi

    step "Stopping the services and removing the containers"
    $COMPOSE down --remove-orphans || true
    ok "The containers and the network were removed."

    step "Removing the data volumes"
    # First the project's named volumes (the project name prefix is set in compose)
    $COMPOSE down --volumes --remove-orphans || true
    for vol in pgdata redisdata backups wgguard_pgdata wgguard_redisdata wgguard_backups; do
        if docker volume inspect "$vol" >/dev/null 2>&1; then
            docker volume rm -f "$vol" >/dev/null 2>&1 && ok "Volume \"$vol\" was removed." || warn "Removing volume \"$vol\" failed."
        fi
    done

    step "Removing the backups directory on disk"
    if [ -d "./backups" ]; then
        # We only delete the path we verified
        resolved="$(cd ./backups 2>/dev/null && pwd || true)"
        if [ -n "$resolved" ] && [ "$resolved" = "$(pwd)/backups" ]; then
            rm -rf -- "$resolved"
            ok "The backups directory was removed: $resolved"
        else
            warn "The backups directory path was not what we expected; it was not removed for safety: ${resolved:-?}"
        fi
    else
        dim "  The backups directory did not exist."
    fi

    if [ "$KEEP_IMAGES" != "true" ]; then
        step "Removing the bot image"
        docker image rm -f "${IMAGE_NAME:-wgguard-bot}:${IMAGE_TAG:-latest}" >/dev/null 2>&1 || true
        ok "The image was removed."
    fi

    say ""
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    printf '%s  🗑  The complete removal is done.%s\n' "$C_RED" "$C_RESET"
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    say ""
    info "The .env file is still on disk (it contains the secrets). If you do not need it, delete it yourself:"
    dim "    rm -f $INSTALL_DIR/.env"
    say ""
    info "To install again from scratch:"
    dim "    cd $INSTALL_DIR && bash install.sh"
    say ""
    return 0
}

main "$@"
