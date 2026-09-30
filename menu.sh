#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — control menu
#
#  One entry point for everything an operator does by hand:
#    install · update · status · live logs · backup · restore · domain & SSL ·
#    panel password · .env repair · maintenance · uninstall
#
#  Interactive:      bash menu.sh
#  One command only: bash menu.sh status | logs bot | backup | restore | …
#                     (bash menu.sh --help lists them all)
#
#  English only, on purpose: a Linux terminal has no bidi support and renders
#  Persian backwards, so an operator cannot act on a Persian line (AGENTS.md §6).
#  The single exception is the «بله» answer the y/n prompts still accept.
# ---------------------------------------------------------------------------
set -euo pipefail

# ---------------------------------------------------------------------------
#  Globals
# ---------------------------------------------------------------------------
INSTALL_DIR=""
ASSUME_YES="false"
ASCII="false"
COMMAND=""
COMMAND_ARGS=()

PROJECT_VERSION="1.0.0"
BOX_W=68           # visible width of the header box
HINT_COL=44        # column where a menu item's hint starts
LOG_TAIL=100
BACKUP_KEEP_DEFAULT=7

#: Where the released operator scripts live.  Used only when there is no
#: checkout to run them from — an operator who pasted the one-liner from the
#: README has no project directory yet, and item 1 has to still work.
RAW_BASE="${WGGB_RAW_BASE:-https://raw.githubusercontent.com/Sir-Adnan/wg-guard-bot/main}"

COMPOSE=""
COMPOSE_TLS=""

# ---------------------------------------------------------------------------
#  Palette
# ---------------------------------------------------------------------------
C_RESET=""; C_BOLD=""; C_DIM=""
C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_MAGENTA=""; C_CYAN=""; C_WHITE=""

init_colors() {
    if [ -z "${NO_COLOR:-}" ] && [ -t 1 ]; then
        C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
        C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
        C_BLUE=$'\033[34m'; C_MAGENTA=$'\033[35m'; C_CYAN=$'\033[36m'; C_WHITE=$'\033[97m'
    fi
}

# Box-drawing and status glyphs.  --ascii swaps them out, and so does a machine
# without a UTF-8 locale — the width maths below counts characters, so a
# byte-oriented locale would leave the box borders ragged.
GLYPH_TL="╭"; GLYPH_TR="╮"; GLYPH_BL="╰"; GLYPH_BR="╯"
GLYPH_H="─"; GLYPH_V="│"
GLYPH_DOT="●"; GLYPH_RING="○"; GLYPH_ARROW="❯"
GLYPH_OK="✔"; GLYPH_WARN="⚠"; GLYPH_ERR="✖"; GLYPH_BULLET="▸"; GLYPH_DIAMOND="◆"

use_ascii_glyphs() {
    GLYPH_TL="+"; GLYPH_TR="+"; GLYPH_BL="+"; GLYPH_BR="+"
    GLYPH_H="-"; GLYPH_V="|"
    GLYPH_DOT="*"; GLYPH_RING="o"; GLYPH_ARROW=">"
    GLYPH_OK="+"; GLYPH_WARN="!"; GLYPH_ERR="x"; GLYPH_BULLET=">"; GLYPH_DIAMOND="*"
}

ensure_utf8_locale() {
    case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
        *UTF-8*|*utf-8*|*UTF8*|*utf8*) return 0 ;;
    esac
    if command -v locale >/dev/null 2>&1; then
        local found
        found="$(locale -a 2>/dev/null | grep -iE '^(C|en_US)\.utf-?8$' | head -n1 || true)"
        if [ -n "$found" ]; then
            export LC_ALL="$found"
            return 0
        fi
    fi
    use_ascii_glyphs
    return 0
}

# ---------------------------------------------------------------------------
#  Output helpers (same vocabulary as install.sh / update.sh / uninstall.sh)
# ---------------------------------------------------------------------------
say()  { printf '%s\n' "$*"; }
info() { printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
ok()   { printf '%s%s%s %s\n' "$C_GREEN" "$GLYPH_OK" "$C_RESET" "$*"; }
warn() { printf '%s%s%s  %s\n' "$C_YELLOW" "$GLYPH_WARN" "$C_RESET" "$*" >&2; }
err()  { printf '%s%s%s %s\n' "$C_RED" "$GLYPH_ERR" "$C_RESET" "$*" >&2; }
step() { printf '\n%s%s%s %s%s\n' "$C_BOLD" "$C_BLUE" "$GLYPH_BULLET" "$*" "$C_RESET"; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }
hint() { printf '%s    %s%s\n' "$C_DIM" "$*" "$C_RESET"; }

pause() {
    [ -t 0 ] || return 0
    printf '\n%s  Press Enter to return to the menu…%s' "$C_DIM" "$C_RESET"
    IFS= read -r _ || true
    printf '\n'
}

prompt_read() {
    # prompt_read <variable name> — reads a line into the named variable
    local __var="$1" __value=""
    printf '\n%s%s%s ' "$C_GREEN" "$GLYPH_ARROW" "$C_RESET"
    if ! IFS= read -r __value; then
        printf -v "$__var" '%s' ""
        return 1
    fi
    printf -v "$__var" '%s' "$__value"
    return 0
}

confirm() {
    # confirm <question> [default y|n]
    local question="$1" default="${2:-n}" answer=""
    if [ "$ASSUME_YES" = "true" ]; then
        [ "$default" = "y" ]
        return $?
    fi
    printf '%s? %s%s %s[%s]%s\n  %s%s%s ' \
        "$C_BOLD" "$question" "$C_RESET" "$C_DIM" "$default" "$C_RESET" \
        "$C_GREEN" "$GLYPH_ARROW" "$C_RESET" >&2
    IFS= read -r answer || answer=""
    [ -n "$answer" ] || answer="$default"
    case "$answer" in
        y|Y|yes|YES|بله|ب) return 0 ;;
        *) return 1 ;;
    esac
}

ask() {
    # ask <question> [default] — prints the answer on stdout
    local question="$1" default="${2:-}" answer=""
    printf '%s? %s%s %s[%s]%s\n  %s%s%s ' \
        "$C_BOLD" "$question" "$C_RESET" "$C_DIM" "$default" "$C_RESET" \
        "$C_GREEN" "$GLYPH_ARROW" "$C_RESET" >&2
    IFS= read -r answer || answer=""
    [ -n "$answer" ] || answer="$default"
    printf '%s' "$answer"
}

# ---------------------------------------------------------------------------
#  Layout: width maths that ignores colour escapes
# ---------------------------------------------------------------------------
_visible() {
    printf '%s' "$1" | sed -E $'s/\033\\[[0-9;]*[A-Za-z]//g'
}

_vlen() {
    local text
    text="$(_visible "$1")"
    printf '%s' "${#text}"
}

_pad() {
    # _pad <text> <width> — right-pad to a visible width
    local text="$1" width="$2" fill
    fill=$(( width - $(_vlen "$text") ))
    [ "$fill" -gt 0 ] || fill=0
    printf '%s%*s' "$text" "$fill" ""
}

repeat_glyph() {
    # Repeat a glyph N times.  Not `printf | tr`: tr replaces byte by byte, so a
    # multi-byte glyph comes out as a run of its first byte (verified: '─' → E2 E2 E2).
    local glyph="$1" count="${2:-0}" line=""
    is_number "$count" || count=0
    [ "$count" -gt 0 ] || return 0
    printf -v line '%*s' "$count" ""
    printf '%s' "${line// /$glyph}"
}

box_top() {
    printf '%s%s%s%s%s\n' "$C_MAGENTA" "$GLYPH_TL" \
        "$(repeat_glyph "$GLYPH_H" $(( BOX_W - 2 )))" "$GLYPH_TR" "$C_RESET"
}

box_bottom() {
    printf '%s%s%s%s%s\n' "$C_MAGENTA" "$GLYPH_BL" \
        "$(repeat_glyph "$GLYPH_H" $(( BOX_W - 2 )))" "$GLYPH_BR" "$C_RESET"
}

box_row() {
    printf '%s%s%s  %s  %s%s%s\n' \
        "$C_MAGENTA" "$GLYPH_V" "$C_RESET" \
        "$(_pad "${1:-}" $(( BOX_W - 6 )))" \
        "$C_MAGENTA" "$GLYPH_V" "$C_RESET"
}

rule() {
    # a section heading: "─── label ───────"
    local label="$1" text pad
    text=" $label "
    pad=$(( BOX_W - ${#text} ))
    [ "$pad" -gt 0 ] || pad=0
    printf '\n%s%s%s%s%s%s\n' "$C_BOLD" "$C_BLUE" "$text" "$C_DIM" \
        "$(repeat_glyph "$GLYPH_H" "$pad")" "$C_RESET"
}

menu_item() {
    local num="$1" label="$2" note="${3:-}" base
    base="$(printf '   %2s   %s' "$num" "$label")"
    if [ -n "$note" ]; then
        base="$(_pad "$base" "$HINT_COL")${C_DIM}${note}${C_RESET}"
    fi
    printf '%s\n' "$base"
}

# ---------------------------------------------------------------------------
#  Project and environment
# ---------------------------------------------------------------------------
project_version() {
    local v=""
    if [ -f "$INSTALL_DIR/pyproject.toml" ]; then
        v="$(grep -E '^version[[:space:]]*=' "$INSTALL_DIR/pyproject.toml" | head -n1 \
            | sed -e 's/.*=[[:space:]]*//' -e 's/"//g' -e "s/'//g" || true)"
    fi
    [ -n "$v" ] || v="1.0.0"
    printf '%s' "$v"
}

detect_dir() {
    # Where the project lives, in the order an operator expects:
    #   --dir  ·  the current directory  ·  this script's own directory  ·
    #   the installer's default location
    #
    # The script's own directory is skipped when it is a process substitution —
    # `bash <(curl …/menu.sh)` hands us /dev/fd/63, which is not a directory
    # anybody can act on.  Treating it as one is why item 2 answered "this
    # server is not installed yet" on a server that was installed.
    #
    # When nothing is found we fall back to the directory an install would use,
    # so every later step has a real path to talk about.
    if [ -z "$INSTALL_DIR" ]; then
        local self="${BASH_SOURCE[0]:-$0}" self_dir="" candidate
        case "$self" in
            /dev/fd/*|/proc/*/fd/*|/dev/stdin|-) self="" ;;
            *) [ -f "$self" ] || self="" ;;
        esac
        if [ -n "$self" ]; then
            self_dir="$(cd -- "$(dirname -- "$self")" 2>/dev/null && pwd -P || true)"
        fi

        for candidate in "$(pwd)" "$self_dir" "${HOME:-/root}/wg-guard-bot" "/root/wg-guard-bot" "/opt/wg-guard-bot"; do
            [ -n "$candidate" ] || continue
            if [ -f "$candidate/docker-compose.yml" ]; then
                INSTALL_DIR="$candidate"
                break
            fi
        done
        [ -n "$INSTALL_DIR" ] || INSTALL_DIR="${HOME:-/root}/wg-guard-bot"
    fi
    INSTALL_DIR="${INSTALL_DIR%/}"
    # A directory that does not exist yet (a fresh server) must not trip `set -e`
    # into exiting before anything is printed.
    if [ -n "$INSTALL_DIR" ] && [ -d "$INSTALL_DIR" ]; then
        cd "$INSTALL_DIR" 2>/dev/null || true
    fi
    return 0
}

self_path() {
    # A path to this script that another process (sudo) can open, or "" when it
    # arrived through a pipe and has none.
    local self="${BASH_SOURCE[0]:-$0}" dir=""
    case "$self" in
        /dev/fd/*|/proc/*/fd/*|/dev/stdin|-) return 1 ;;
    esac
    [ -f "$self" ] || return 1
    dir="$(cd -- "$(dirname -- "$self")" 2>/dev/null && pwd -P || true)"
    if [ -n "$dir" ]; then
        printf '%s/%s' "$dir" "$(basename -- "$self")"
    else
        printf '%s' "$self"
    fi
    return 0
}

fetch_released() {
    # fetch_released <script> — download a sibling script from RAW_BASE into a
    # temporary file and print its path; returns 1 when that is impossible.
    local script="$1" tmp=""
    command -v curl >/dev/null 2>&1 || return 1
    tmp="$(mktemp "${TMPDIR:-/tmp}/wgguard-${script%.sh}-XXXXXX.sh" 2>/dev/null || true)"
    [ -n "$tmp" ] || return 1
    if curl -fsSL "$RAW_BASE/$script" -o "$tmp" 2>/dev/null; then
        chmod +x "$tmp" 2>/dev/null || true
        printf '%s' "$tmp"
        return 0
    fi
    rm -f "$tmp"
    return 1
}

require_root() {
    [ "$(id -u)" -eq 0 ] && return 0

    local self=""
    if [ -f "$INSTALL_DIR/menu.sh" ]; then
        self="$INSTALL_DIR/menu.sh"
    else
        self="$(self_path || true)"
    fi
    if [ -z "$self" ]; then
        # `bash <(curl …/menu.sh)`: /dev/fd/63 belongs to this shell, so sudo
        # cannot read it back.  The released copy can be read by anyone.
        local tmp=""
        tmp="$(fetch_released menu.sh || true)"
        if [ -n "$tmp" ]; then
            self="$tmp"
            dim "  This copy of the menu arrived through a pipe; running the released copy under sudo."
        fi
    fi

    if command -v sudo >/dev/null 2>&1; then
        if [ -z "$self" ]; then
            err "This menu manages Docker, so it needs root — and this copy has no path to re-run."
            hint "Download it first:  curl -fsSL $RAW_BASE/menu.sh -o menu.sh && sudo bash menu.sh"
            exit 1
        fi
        warn "This menu manages Docker, so it needs root; re-running it with sudo…"
        exec sudo -E bash "$self" "$@"
    fi
    err "Root access is required (and sudo is not installed)."
    exit 1
}

env_file() { printf '%s/.env' "$INSTALL_DIR"; }

is_installed() { [ -f "$(env_file)" ]; }

env_get() {
    # env_get <key> [file] — the value without quotes; empty when absent
    local key="$1" file="${2:-$(env_file)}" line=""
    [ -f "$file" ] || return 0
    line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n1 || true)"
    [ -n "$line" ] || return 0
    line="${line#*=}"
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    case "$line" in
        \"*\") line="${line#\"}"; line="${line%\"}" ;;
        \'*\') line="${line#\'}"; line="${line%\'}" ;;
    esac
    printf '%s' "$line"
}

env_has() {
    # env_has <key> [file]
    local key="$1" file="${2:-$(env_file)}"
    [ -f "$file" ] && grep -Eq "^[[:space:]]*${key}=" "$file"
}

env_set() {
    # env_set <file> <key> <value> — replaces the line in place, or appends it
    local file="$1" key="$2" value="$3" escaped
    if grep -Eq "^[[:space:]]*${key}=" "$file"; then
        escaped="$(printf '%s' "$value" | sed -e 's/[&\\|]/\\&/g')"
        sed -i "s|^[[:space:]]*${key}=.*|${key}=${escaped}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

panel_url() {
    local url port
    url="$(env_get PANEL_BASE_URL)"
    if [ -z "$url" ]; then
        port="$(env_get PANEL_PORT)"
        [ -n "$port" ] || port="8080"
        url="http://127.0.0.1:${port}"
    fi
    printf '%s' "$url"
}

panel_port() {
    local port
    port="$(env_get PANEL_PORT)"
    [ -n "$port" ] || port="8080"
    printf '%s' "$port"
}

domain_value() { env_get DOMAIN; }

backup_dir() { printf '%s/backups' "$INSTALL_DIR"; }

git_short() {
    command -v git >/dev/null 2>&1 || return 0
    [ -d "$INSTALL_DIR/.git" ] || return 0
    git -C "$INSTALL_DIR" rev-parse --short HEAD 2>/dev/null || true
}

git_dirty() {
    # tracked edits only: untracked files (.env, backup dumps) never block an update
    [ -d "$INSTALL_DIR/.git" ] || return 1
    [ -n "$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no 2>/dev/null || true)" ]
}

# ---------------------------------------------------------------------------
#  Docker
# ---------------------------------------------------------------------------
docker_present() { command -v docker >/dev/null 2>&1; }

detect_compose() {
    COMPOSE=""
    docker_present || return 1
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
        COMPOSE_TLS="docker compose --profile tls"
        return 0
    fi
    return 1
}

require_docker() {
    if ! docker_present; then
        err "Docker is not installed on this server."
        hint "Install it:  bash menu.sh install"
        return 1
    fi
    if ! detect_compose; then
        err "Docker Compose v2 was not found."
        hint "apt-get update && apt-get install -y docker-compose-plugin"
        return 1
    fi
    if ! docker info >/dev/null 2>&1; then
        err "The Docker daemon is not responding."
        hint "Start it:  systemctl start docker"
        return 1
    fi
    return 0
}

require_installed() {
    if ! is_installed; then
        err "This server is not installed yet — no .env in $INSTALL_DIR"
        hint "Set it up with:  bash menu.sh install"
        return 1
    fi
    return 0
}

container_for() {
    case "$1" in
        bot|app)     printf 'wgguard-bot' ;;
        db|postgres) printf 'wgguard-db' ;;
        redis)       printf 'wgguard-redis' ;;
        caddy|tls)   printf 'wgguard-caddy' ;;
        *)           printf '%s' "$1" ;;
    esac
}

container_state() {
    # running | exited | created | absent
    local name="$1" state=""
    docker_present || { printf 'absent'; return 0; }
    state="$(docker ps -a --filter "name=^${name}\$" --format '{{.State}}' 2>/dev/null | head -n1 || true)"
    [ -n "$state" ] || state="absent"
    printf '%s' "$state"
}

container_running() { [ "$(container_state "$1")" = "running" ]; }

service_badge() {
    local name="$1" label="$2" state
    state="$(container_state "$name")"
    case "$state" in
        running) printf '%s%s%s %s' "$C_GREEN" "$GLYPH_DOT" "$C_RESET" "$label" ;;
        absent)  printf '%s%s%s %s' "$C_DIM" "$GLYPH_RING" "$C_RESET" "$label" ;;
        *)       printf '%s%s%s %s' "$C_RED" "$GLYPH_RING" "$C_RESET" "$label" ;;
    esac
}

health_json() {
    curl -fsS --max-time 2 "http://127.0.0.1:$(panel_port)/healthz" 2>/dev/null || true
}

live_revision() {
    local json
    json="$(health_json)"
    [ -n "$json" ] || return 0
    printf '%s' "$json" | sed -n 's/.*"commit":"\([^"]*\)".*/\1/p'
}

wait_health() {
    # wait_health [timeout seconds]
    local timeout="${1:-120}" waited=0 url
    url="http://127.0.0.1:$(panel_port)/healthz"
    command -v curl >/dev/null 2>&1 || { sleep 5; return 0; }
    while [ "$waited" -lt "$timeout" ]; do
        if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 3
        waited=$(( waited + 3 ))
    done
    return 1
}

db_user() { local v; v="$(env_get POSTGRES_USER)"; [ -n "$v" ] || v="wgguard"; printf '%s' "$v"; }
db_name() { local v; v="$(env_get POSTGRES_DB)";   [ -n "$v" ] || v="wgguard"; printf '%s' "$v"; }

# ---------------------------------------------------------------------------
#  Small shared helpers
# ---------------------------------------------------------------------------
is_number() { case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac; return 0; }

human_size() {
    local bytes="${1:-0}"
    is_number "$bytes" || bytes=0
    if [ "$bytes" -ge 1048576 ]; then
        printf '%s MB' "$(( bytes / 1048576 ))"
    elif [ "$bytes" -ge 1024 ]; then
        printf '%s KB' "$(( bytes / 1024 ))"
    else
        printf '%s B' "$bytes"
    fi
}

file_size() {
    local path="$1" bytes=0
    if [ -f "$path" ]; then
        bytes="$(wc -c < "$path" | tr -d ' ')"
    fi
    is_number "$bytes" || bytes=0
    human_size "$bytes"
}

total_bytes() {
    # total_bytes <file…> — GNU find output is passed in, so sizes only
    local total
    total="$(awk '{ sum += $1 } END { printf "%d", sum + 0 }')"
    printf '%s' "$total"
}

run_child() {
    # run_child <script> [args…] — run a sibling script inside the project
    # directory.  Without a checkout (the curl one-liner on a fresh server) the
    # released copy is fetched and run in its place, so an item never dead-ends
    # on "install.sh was not found in /dev/fd".
    #
    # The child's exit status is returned, never fatal: a failed update has to
    # come back to the menu with "the update did not finish", not end the
    # session (which is what `set -e` does to a bare failing command).
    local script="$1" tmp="" status=0
    shift
    if [ -f "$INSTALL_DIR/$script" ]; then
        ( cd "$INSTALL_DIR" && bash "$script" "$@" ) || status=$?
        return $status
    fi

    tmp="$(fetch_released "$script" || true)"
    if [ -z "$tmp" ]; then
        err "$script was not found in $INSTALL_DIR"
        hint "Clone the project and run the menu from inside it:"
        hint "  git clone https://github.com/Sir-Adnan/wg-guard-bot.git && cd wg-guard-bot && bash menu.sh"
        return 1
    fi
    dim "  $script is not in $INSTALL_DIR; running the released copy instead."
    # install.sh creates the directory itself; everything else runs from inside it.
    if [ -d "$INSTALL_DIR" ]; then
        ( cd "$INSTALL_DIR" && bash "$tmp" "$@" ) || status=$?
    else
        bash "$tmp" "$@" || status=$?
    fi
    rm -f "$tmp"
    return $status
}

# ===========================================================================
#  Actions
# ===========================================================================

# -- 1) install -------------------------------------------------------------
act_install() {
    step "Install / reinstall"
    if ! docker_present; then
        dim "  Docker is missing; install.sh installs it first."
    fi
    run_child install.sh --dir "$INSTALL_DIR" "$@"
}

# -- 2) update --------------------------------------------------------------
act_update() {
    step "Update to the latest version"
    require_installed || return 1
    run_child update.sh
}

# -- 3) status --------------------------------------------------------------
act_status() {
    step "Status"

    printf '%s  Services%s\n' "$C_BOLD" "$C_RESET"
    if ! docker_present; then
        dim "    Docker is not installed on this server."
    elif ! docker info >/dev/null 2>&1; then
        dim "    The Docker daemon is not reachable (is it running?)."
    else
        local rows line name state detail
        rows="$(docker ps -a --filter "name=^wgguard-" --format '{{.Names}}|{{.State}}|{{.Status}}' 2>/dev/null || true)"
        if [ -z "$rows" ]; then
            dim "    No WG-Guard container exists yet — item 1 installs the stack."
        else
            while IFS= read -r line; do
                [ -n "$line" ] || continue
                name="${line%%|*}"
                line="${line#*|}"
                state="${line%%|*}"
                detail="${line#*|}"
                if [ "$state" = "running" ]; then
                    printf '    %s%s%s %-14s %-8s %s%s%s\n' \
                        "$C_GREEN" "$GLYPH_DOT" "$C_RESET" "$name" "$state" "$C_DIM" "$detail" "$C_RESET"
                else
                    printf '    %s%s%s %-14s %-8s %s%s%s\n' \
                        "$C_RED" "$GLYPH_RING" "$C_RESET" "$name" "$state" "$C_DIM" "$detail" "$C_RESET"
                fi
            done <<< "$rows"
        fi
    fi

    printf '\n%s  Application%s\n' "$C_BOLD" "$C_RESET"
    if ! is_installed; then
        dim "    Not installed yet."
    else
        local json ready dom live checkout
        json="$(health_json)"
        if [ -n "$json" ]; then
            printf '    health      %s%s%s\n' "$C_GREEN" "$json" "$C_RESET"
        else
            printf '    health      %sno answer on 127.0.0.1:%s/healthz%s\n' \
                "$C_YELLOW" "$(panel_port)" "$C_RESET"
        fi

        ready="$(curl -fsS --max-time 3 "http://127.0.0.1:$(panel_port)/readyz" 2>/dev/null || true)"
        if [ -n "$ready" ]; then
            printf '    readiness   %s\n' "$ready"
        else
            printf '    readiness   %sno answer%s\n' "$C_YELLOW" "$C_RESET"
        fi

        printf '    panel       %s%s%s\n' "$C_CYAN" "$(panel_url)" "$C_RESET"
        printf '    login       %s/panel/login\n' "$(panel_url)"

        dom="$(domain_value)"
        if [ -n "$dom" ]; then
            if container_running wgguard-caddy; then
                printf '    domain      %s  %s(TLS through Caddy)%s\n' "$dom" "$C_GREEN" "$C_RESET"
            else
                printf '    domain      %s  %s(Caddy is not running)%s\n' "$dom" "$C_YELLOW" "$C_RESET"
            fi
        else
            printf '    domain      %snone — polling on the server IP%s\n' "$C_DIM" "$C_RESET"
        fi

        live="$(live_revision)"
        checkout="$(git_short)"
        printf '    revision    live %s  ·  checkout %s\n' "${live:-unknown}" "${checkout:-unknown}"
        if [ -n "$live" ] && [ "$live" != "unknown" ] && [ -n "$checkout" ] && [ "$live" != "$checkout" ]; then
            warn "The running image is not the checked-out revision — update (2) or rebuild (10 → 3)."
        fi
        if git_dirty; then
            warn "The project directory has local changes."
        fi
    fi

    printf '\n%s  Backups%s\n' "$C_BOLD" "$C_RESET"
    local dumps
    dumps="$(count_backups)"
    if [ "$dumps" -gt 0 ]; then
        printf '    ./backups   %s file(s) · %s · newest %s\n' \
            "$dumps" "$(human_size "$(backups_total_size)")" "$(newest_backup)"
    else
        printf '    ./backups   %sempty%s\n' "$C_DIM" "$C_RESET"
    fi
    if docker_present && container_running wgguard-bot; then
        local inner
        inner="$($COMPOSE exec -T bot sh -c 'ls -1 /app/backups/*.sql 2>/dev/null | wc -l' 2>/dev/null | tr -d '\r\n ' || true)"
        is_number "$inner" || inner=0
        printf '    in the app  %s automatic dump(s) in /app/backups\n' "$inner"
    fi

    printf '\n%s  Server%s\n' "$C_BOLD" "$C_RESET"
    printf '    project     %s\n' "$INSTALL_DIR"
    printf '    disk        %s\n' "$(df -h "$INSTALL_DIR" 2>/dev/null | awk 'NR==2 {print $5" used · "$4" free"}' || printf 'unknown')"
    printf '    uptime      %s\n' "$(uptime -p 2>/dev/null | sed -e 's/^up //' || printf 'unknown')"
    return 0
}

# -- 4) logs ----------------------------------------------------------------
logs_stream() {
    # logs_stream <follow|tail> <container|all>
    local mode="$1"
    shift
    local services="$*" name
    if [ "$services" = "all" ]; then
        if [ "$mode" = "follow" ]; then
            info "Streaming every service (Ctrl-C returns to the menu)…"
            $COMPOSE logs -f --tail="$LOG_TAIL" || true
        else
            $COMPOSE logs --tail="$LOG_TAIL" || true
        fi
        return 0
    fi
    for name in $services; do
        if [ "$(container_state "$name")" = "absent" ]; then
            err "Container '$name' does not exist yet."
            return 1
        fi
    done
    if [ "$mode" = "follow" ]; then
        info "Streaming $services (Ctrl-C returns to the menu)…"
        # shellcheck disable=SC2086  # word splitting is intended: several containers
        docker logs -f --tail="$LOG_TAIL" $services || true
    else
        info "Last $LOG_TAIL lines of $services"
        # shellcheck disable=SC2086
        docker logs --tail="$LOG_TAIL" $services || true
    fi
}

act_logs() {
    # act_logs [service] [--tail N] [--no-follow]
    local service="" mode="follow" n="$LOG_TAIL"
    if [ "$#" -gt 0 ]; then
        case "$1" in
            -*) : ;;
            *)  service="$1"; shift ;;
        esac
    fi
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --tail)      n="${2:-$LOG_TAIL}"; shift ;;
            --tail=*)    n="${1#*=}" ;;
            --no-follow|--snapshot) mode="tail" ;;
            *)           service="${service:-$1}" ;;
        esac
        shift
    done
    is_number "$n" || n="$LOG_TAIL"
    LOG_TAIL="$n"

    require_docker || return 1
    if [ -z "$service" ]; then
        if [ -t 0 ]; then
            menu_logs
        else
            err "Which logs? Pass a service: bot | db | redis | caddy | all"
            return 2
        fi
        return 0
    fi
    logs_stream "$mode" "$(container_for "$service")"
}

menu_logs() {
    local choice
    while :; do
        rule "Live logs"
        menu_item 1 "Bot"            "live · Ctrl-C to stop"
        menu_item 2 "Bot"            "last $LOG_TAIL lines"
        menu_item 3 "Database"       "live"
        menu_item 4 "Redis"          "live"
        menu_item 5 "Caddy / TLS"    "live"
        menu_item 6 "Everything"     "live · every service"
        menu_item 7 "Everything"     "last $LOG_TAIL lines"
        menu_item 0 "Back to the main menu"
        if ! prompt_read choice; then
            return 0
        fi
        case "$choice" in
            1) logs_stream follow wgguard-bot; pause ;;
            2) logs_stream tail wgguard-bot; pause ;;
            3) logs_stream follow wgguard-db; pause ;;
            4) logs_stream follow wgguard-redis; pause ;;
            5) logs_stream follow wgguard-caddy; pause ;;
            6) logs_stream follow all; pause ;;
            7) logs_stream tail all; pause ;;
            0|q|Q|"") return 0 ;;
            *) warn "Unknown choice: $choice"; pause ;;
        esac
    done
}

# -- 5) backup --------------------------------------------------------------
act_backup() {
    step "Backing up the database and .env"
    require_installed || return 1
    require_docker || return 1
    if ! container_running wgguard-db; then
        err "The database container is not running."
        hint "Start the stack first:  $COMPOSE up -d"
        return 1
    fi

    local dir stamp dump envcopy errfile count keep
    dir="$(backup_dir)"
    stamp="$(date +%Y%m%d-%H%M%S)"
    dump="$dir/wgguard-${stamp}.sql"
    envcopy="$dir/wgguard-${stamp}.env"
    errfile="$(mktemp)"

    mkdir -p "$dir"
    chmod 700 "$dir" 2>/dev/null || true

    dim "  pg_dump → $dump"
    if ! $COMPOSE exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' \
        > "$dump" 2>"$errfile"; then
        err "pg_dump failed: $(head -n2 "$errfile" | tr '\n' ' ')"
        rm -f "$dump" "$errfile"
        return 1
    fi
    rm -f "$errfile"
    if [ ! -s "$dump" ]; then
        err "The dump came back empty and was removed — nothing was backed up."
        rm -f "$dump"
        return 1
    fi

    if [ -f "$(env_file)" ]; then
        cp -p "$(env_file)" "$envcopy"
        chmod 600 "$envcopy" 2>/dev/null || true
    fi

    ok "Database: $dump  ($(file_size "$dump"))"
    if [ -f "$envcopy" ]; then
        ok "Settings: $envcopy  ($(file_size "$envcopy"))"
        dim "  That file contains SECRET_KEY — keep it as private as .env itself."
    fi

    count="$(count_backups)"
    keep="$(env_get BACKUP_KEEP)"
    is_number "$keep" || keep="$BACKUP_KEEP_DEFAULT"
    dim "  $count dump(s) in $dir — item 10 → 4 prunes all but the newest $keep."
    return 0
}

list_backups() {
    # newest first, one absolute path per line (empty when there is no ./backups)
    local dir
    dir="$(backup_dir)"
    [ -d "$dir" ] || return 0
    find "$dir" -maxdepth 1 -name 'wgguard-*.sql' -printf '%T@ %p\n' 2>/dev/null \
        | sort -rn | cut -d' ' -f2- || true
}

count_backups() {
    local n
    n="$(list_backups | wc -l | tr -d ' ' || printf '0')"
    is_number "$n" || n=0
    printf '%s' "$n"
}

backups_total_size() {
    local dir
    dir="$(backup_dir)"
    [ -d "$dir" ] || { printf '0'; return 0; }
    find "$dir" -maxdepth 1 -name 'wgguard-*.sql' -printf '%s\n' 2>/dev/null | total_bytes || printf '0'
}

newest_backup() {
    local dir
    dir="$(backup_dir)"
    [ -d "$dir" ] || return 0
    find "$dir" -maxdepth 1 -name 'wgguard-*.sql' -printf '%TY-%Tm-%Td %TH:%TM\n' 2>/dev/null \
        | sort | tail -n1 || true
}

container_backups() {
    container_running wgguard-bot || return 0
    $COMPOSE exec -T bot sh -c 'ls -1t /app/backups/*.sql 2>/dev/null' 2>/dev/null | tr -d '\r' || true
}

pick_backup() {
    # prints the chosen dump path; prints nothing when the operator backs out
    local -a host_files=() inner_files=()
    local line choice i path idx remote target

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        host_files+=("$line")
    done < <(list_backups)

    while IFS= read -r line; do
        [ -n "$line" ] || continue
        inner_files+=("$line")
    done < <(container_backups)

    if [ "${#host_files[@]}" -eq 0 ] && [ "${#inner_files[@]}" -eq 0 ]; then
        err "No backup was found."
        hint "Create one with item 5."
        return 1
    fi

    rule "Available backups"
    i=1
    if [ "${#host_files[@]}" -gt 0 ]; then
        dim "  on this server — $(backup_dir)"
        for path in "${host_files[@]}"; do
            printf '   %2s   %-34s %s\n' "$i" "$(basename "$path")" "$(file_size "$path")"
            i=$(( i + 1 ))
        done
    fi
    if [ "${#inner_files[@]}" -gt 0 ]; then
        dim "  inside the app container (/app/backups — the automatic dumps)"
        for path in "${inner_files[@]}"; do
            printf '   %2s   %-34s %s\n' "$i" "$(basename "$path")" "in the container"
            i=$(( i + 1 ))
        done
    fi
    printf '   %2s   %s\n' "0" "Back"

    choice="$(ask "Which backup?" "")"
    case "$choice" in
        ""|0|q|Q) return 1 ;;
    esac
    if ! is_number "$choice"; then
        err "Enter the number of a backup."
        return 1
    fi
    if [ "$choice" -lt 1 ] || [ "$choice" -ge "$i" ]; then
        err "There is no backup number $choice."
        return 1
    fi

    if [ "$choice" -le "${#host_files[@]}" ]; then
        printf '%s' "${host_files[$(( choice - 1 ))]}"
        return 0
    fi

    idx=$(( choice - ${#host_files[@]} - 1 ))
    remote="${inner_files[$idx]}"
    target="$(backup_dir)/$(basename "$remote")"
    mkdir -p "$(backup_dir)"
    dim "  copying $(basename "$remote") out of the container…"
    if ! $COMPOSE cp "bot:${remote}" "$target" >/dev/null 2>&1; then
        err "Copying $remote out of the container failed."
        return 1
    fi
    ok "Copied to $target"
    printf '%s' "$target"
}

act_restore() {
    # act_restore [file]
    local file="${1:-}"
    step "Restoring a backup"
    require_installed || return 1
    require_docker || return 1

    if ! container_running wgguard-db; then
        err "The database container is not running."
        hint "Start the stack first:  $COMPOSE up -d"
        return 1
    fi

    if [ -z "$file" ]; then
        file="$(pick_backup)" || return 0
    fi
    if [ ! -f "$file" ]; then
        err "That file does not exist: $file"
        return 1
    fi
    if [ ! -s "$file" ]; then
        err "That file is empty: $file"
        return 1
    fi

    printf '\n'
    printf '  file      %s\n' "$file"
    printf '  size      %s\n' "$(file_size "$file")"
    printf '  database  %s (user %s)\n' "$(db_name)" "$(db_user)"

    if confirm "Take a safety backup of the current database first?" "y"; then
        act_backup || warn "The safety backup failed; carrying on without it."
    fi

    warn "Restoring REPLACES the current database — anything missing from the dump is gone."
    if ! confirm "Restore $(basename "$file") now?" "n"; then
        dim "  Cancelled — nothing was changed."
        return 0
    fi

    step "Restoring"
    dim "  stopping the bot so nothing writes while the schema is replaced…"
    $COMPOSE stop bot >/dev/null 2>&1 || true

    dim "  dropping and recreating the public schema…"
    if ! $COMPOSE exec -T db psql -U "$(db_user)" -d "$(db_name)" -v ON_ERROR_STOP=1 \
        -c 'DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;' >/dev/null; then
        err "Resetting the schema failed. The bot stays stopped; the database is as it was."
        $COMPOSE start bot >/dev/null 2>&1 || true
        return 1
    fi

    dim "  loading $(basename "$file")…"
    if ! $COMPOSE exec -T db psql -U "$(db_user)" -d "$(db_name)" -v ON_ERROR_STOP=1 < "$file" >/dev/null; then
        err "Restoring failed — the database is incomplete."
        hint "Restore an older dump, or read:  $COMPOSE logs --tail=40 db"
        $COMPOSE start bot >/dev/null 2>&1 || true
        return 1
    fi

    dim "  starting the bot (migrations run on startup)…"
    $COMPOSE start bot >/dev/null 2>&1 || $COMPOSE up -d bot

    if wait_health 120; then
        ok "Restore finished; the panel answers on $(panel_url)"
    else
        warn "The panel did not answer within 120 seconds. Check:  $COMPOSE logs --tail=40 bot"
    fi
    return 0
}

# -- 6) domain and SSL ------------------------------------------------------
act_domain() {
    # act_domain [status|set|renew|disable] [domain]
    local mode="${1:-}" value="${2:-}" choice
    if [ -z "$mode" ]; then
        if [ ! -t 0 ]; then
            mode="status"
        else
            rule "Domain and SSL"
            menu_item 1 "Show the domain and certificate status"
            menu_item 2 "Set or change the domain"   "the certificate is issued automatically"
            menu_item 3 "Renew the certificate now"
            menu_item 4 "Turn HTTPS off"             "keeps polling on the server IP"
            menu_item 0 "Back to the main menu"
            prompt_read choice || return 0
            case "$choice" in
                1) mode="status" ;;
                2) mode="set" ;;
                3) mode="renew" ;;
                4) mode="disable" ;;
                0|q|Q|"") return 0 ;;
                *) warn "Unknown choice: $choice"; return 0 ;;
            esac
        fi
    fi

    case "$mode" in
        status)  run_child scripts/set-domain.sh --status ;;
        renew)   run_child scripts/set-domain.sh --renew ;;
        disable) run_child scripts/set-domain.sh --disable ;;
        set)
            [ -n "$value" ] || value="$(ask "Domain or subdomain (for example bot.example.com)" "$(domain_value)")"
            if [ -z "$value" ]; then
                dim "  No domain given — nothing changed."
                return 0
            fi
            run_child scripts/set-domain.sh "$value"
            ;;
        *) err "Unknown domain action: $mode"; return 1 ;;
    esac
}

# -- 7) panel password ------------------------------------------------------
act_password() {
    local login="${1:-}" generate="true"
    step "Panel login"
    require_installed || return 1
    require_docker || return 1
    if ! container_running wgguard-bot; then
        err "The bot container is not running."
        return 1
    fi
    [ -n "$login" ] || login="$(env_get OWNER_USERNAME)"
    [ -n "$login" ] || login="admin"

    if [ -t 0 ] && [ "$ASSUME_YES" != "true" ]; then
        if ! confirm "Generate a strong password automatically?" "y"; then
            generate="false"
        fi
    fi

    if [ "$generate" = "true" ]; then
        $COMPOSE exec -T bot python -m app.cli set-password --login "$login" --generate
    else
        dim "  You are asked for the new password twice; it is never echoed."
        $COMPOSE exec -it bot python -m app.cli set-password --login "$login"
    fi
    ok "Sign in at $(panel_url)/panel/login"
    return 0
}

# -- 8) .env in English -----------------------------------------------------
# Arabic script occupies U+0600–U+07FF, which is the byte range 0xD8–0xDB in
# UTF-8.  Detecting it by byte is deliberate: grep -P is not available everywhere,
# and the English template legitimately contains em-dashes and curly quotes.
RTL_BYTES=$'[\xd8-\xdb]'

arabic_lines() {
    # lines with Persian/Arabic text, minus the APP_NAME value — the shop name
    # customers read, which stays Persian on purpose
    [ -f "$1" ] || return 0
    LC_ALL=C grep -n "$RTL_BYTES" "$1" 2>/dev/null | grep -v ':[[:space:]]*APP_NAME=' || true
}

act_env_english() {
    step "Rewriting .env in English (every value is kept)"
    require_installed || return 1

    local envf tmpl backup new offenders key value count=0 added=0 extras=""
    envf="$(env_file)"
    tmpl="$INSTALL_DIR/.env.example"

    if [ ! -f "$tmpl" ]; then
        err ".env.example was not found in $INSTALL_DIR"
        return 1
    fi

    offenders="$(arabic_lines "$envf")"
    if [ -z "$offenders" ]; then
        ok ".env has no Persian left in it — nothing to do."
        dim "  (APP_NAME keeps its Persian value: that is the shop name customers see.)"
        return 0
    fi

    printf '\n'
    printf '  %s line(s) contain Persian, which a Linux terminal renders backwards:\n' \
        "$(printf '%s\n' "$offenders" | wc -l | tr -d ' ')"
    printf '%s\n' "$offenders" | head -n 3 | awk '{ print "    " substr($0, 1, 100) }'
    say ""
    dim "  Only the comments change. Every value — SECRET_KEY, passwords, tokens —"
    dim "  is copied across exactly as it is, and the old file is kept as a backup."

    if ! confirm "Rewrite .env from the English template?" "y"; then
        dim "  Cancelled — .env was not touched."
        return 0
    fi

    backup="$INSTALL_DIR/.env.bak.$(date +%Y%m%d-%H%M%S)"
    if ! cp -p "$envf" "$backup"; then
        err "Could not write the backup next to .env."
        return 1
    fi
    chmod 600 "$backup" 2>/dev/null || true

    new="$INSTALL_DIR/.env.new.$$"
    cp "$tmpl" "$new"

    # every key the operator already has wins over the template's default
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        if env_has "$key" "$envf"; then
            value="$(env_get "$key" "$envf")"
            env_set "$new" "$key" "$value"
            count=$(( count + 1 ))
        fi
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$tmpl" | cut -d= -f1)

    # keys that only exist in this .env are kept at the end
    while IFS= read -r key; do
        [ -n "$key" ] || continue
        if ! grep -Eq "^[[:space:]]*${key}=" "$tmpl"; then
            extras="${extras}${key}"$'\n'
        fi
    done < <(grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$envf" | cut -d= -f1)

    if [ -n "$extras" ]; then
        {
            printf '\n# ---------------------------------------------------------------------------\n'
            printf '# Values that were in your .env but are not part of .env.example\n'
            printf '# ---------------------------------------------------------------------------\n'
        } >> "$new"
        while IFS= read -r key; do
            [ -n "$key" ] || continue
            env_set "$new" "$key" "$(env_get "$key" "$envf")"
            added=$(( added + 1 ))
        done <<< "$extras"
    fi

    chmod 600 "$new" 2>/dev/null || true
    if ! mv -f "$new" "$envf"; then
        err "Replacing .env failed; the original file is untouched."
        rm -f "$new"
        return 1
    fi
    chmod 600 "$envf" 2>/dev/null || true

    ok "$count value(s) carried over, $added extra key(s) appended."
    ok "Previous file kept as $(basename "$backup")"
    dim "  Any setting that was missing from the old file now comes from the template."

    if docker_present && detect_compose && container_running wgguard-bot; then
        if confirm "Restart the services so the new file is applied?" "y"; then
            $COMPOSE up -d --remove-orphans
            wait_health 120 || warn "The panel did not answer within 120 seconds."
        fi
    fi
    return 0
}

# -- 9) maintenance ---------------------------------------------------------
act_maintenance() {
    # act_maintenance [migrate|restart|rebuild|prune|images|shell|psql]
    local mode="${1:-}" choice
    if [ -z "$mode" ]; then
        if [ ! -t 0 ]; then
            err "Which maintenance action? migrate | restart | rebuild | prune | images | shell | psql"
            return 2
        fi
        rule "Maintenance"
        menu_item 1 "Run the database migrations"
        menu_item 2 "Restart the services"                "keeps the current image"
        menu_item 3 "Rebuild from source and recreate"    "what an update does"
        menu_item 4 "Prune old backups"                   "keeps the newest BACKUP_KEEP"
        menu_item 5 "Remove unused Docker images"
        menu_item 6 "Open a shell in the container"
        menu_item 7 "Open psql on the database"
        menu_item 0 "Back to the main menu"
        prompt_read choice || return 0
        case "$choice" in
            1) mode="migrate" ;;
            2) mode="restart" ;;
            3) mode="rebuild" ;;
            4) mode="prune" ;;
            5) mode="images" ;;
            6) mode="shell" ;;
            7) mode="psql" ;;
            0|q|Q|"") return 0 ;;
            *) warn "Unknown choice: $choice"; return 0 ;;
        esac
    fi

    require_installed || return 1
    require_docker || return 1

    case "$mode" in
        migrate)
            step "Database migrations"
            $COMPOSE exec -T bot alembic upgrade head
            ok "The database is at the latest revision."
            ;;
        restart)
            step "Restarting the services"
            $COMPOSE restart
            wait_health 120 || warn "The panel did not answer within 120 seconds."
            ok "Services restarted."
            ;;
        rebuild)
            step "Rebuilding the image from source"
            local commit=""
            commit="$(git_short)"
            if [ -n "$commit" ]; then
                GIT_COMMIT="$commit" $COMPOSE build --pull bot
                GIT_COMMIT="$commit" $COMPOSE up -d --remove-orphans
            else
                $COMPOSE build --pull bot
                $COMPOSE up -d --remove-orphans
            fi
            $COMPOSE exec -T bot alembic upgrade head
            wait_health 180 || warn "The panel did not answer within 180 seconds."
            ok "Rebuilt and restarted${commit:+ at revision $commit}."
            ;;
        prune)
            step "Pruning old backups"
            local keep files removed=0 path
            keep="$(env_get BACKUP_KEEP)"
            is_number "$keep" || keep="$BACKUP_KEEP_DEFAULT"
            files="$(count_backups)"
            printf '  %s dump(s), %s — keeping the newest %s\n' \
                "$files" "$(human_size "$(backups_total_size)")" "$keep"
            if [ "$files" -le "$keep" ]; then
                ok "Nothing to remove."
                return 0
            fi
            if ! confirm "Delete the older dumps?" "n"; then
                dim "  Cancelled — nothing was deleted."
                return 0
            fi
            while IFS= read -r path; do
                [ -n "$path" ] || continue
                if rm -f "$path"; then
                    removed=$(( removed + 1 ))
                fi
            done < <(list_backups | tail -n "+$(( keep + 1 ))")
            ok "Removed $removed dump(s); the newest $keep are kept."
            ;;
        images)
            step "Removing unused Docker images"
            docker image prune -f
            ;;
        shell)
            step "Shell inside the bot container"
            dim "  Type 'exit' to come back to the menu."
            $COMPOSE exec bot bash || $COMPOSE exec bot sh
            ;;
        psql)
            step "psql on the database"
            dim "  Type \\q to come back to the menu."
            $COMPOSE exec db psql -U "$(db_user)" -d "$(db_name)"
            ;;
        *) err "Unknown maintenance action: $mode"; return 1 ;;
    esac
    return 0
}

# -- 10) uninstall ----------------------------------------------------------
act_uninstall() {
    # act_uninstall [stop|purge]
    local mode="${1:-}" choice
    if [ -z "$mode" ]; then
        if [ ! -t 0 ]; then
            err "Pass what to do: 'uninstall' stops the containers, 'purge' deletes the data too."
            return 2
        fi
        rule "Uninstall"
        menu_item 1 "Stop and remove the containers"  "the data stays"
        menu_item 2 "Delete everything"               "database, Redis, backups — no way back"
        menu_item 0 "Back to the main menu"
        prompt_read choice || return 0
        case "$choice" in
            1) mode="stop" ;;
            2) mode="purge" ;;
            0|q|Q|"") return 0 ;;
            *) warn "Unknown choice: $choice"; return 0 ;;
        esac
    fi

    case "$mode" in
        stop)
            step "Stopping and removing the containers"
            dim "  The database, Redis and the backups stay on this server."
            run_child uninstall.sh
            ;;
        purge)
            step "Deleting everything"
            warn "This removes the database, Redis and every backup — there is no way back."
            if confirm "Take a backup before deleting everything?" "y"; then
                act_backup || warn "The backup failed; continuing anyway."
            fi
            run_child uninstall.sh --purge
            ;;
        *) err "Unknown uninstall action: $mode"; return 1 ;;
    esac
}

# ===========================================================================
#  The home screen
# ===========================================================================
render_home() {
    local installed="false" live="" checkout="" running_rev
    is_installed && installed="true"
    checkout="$(git_short)"
    if [ "$installed" = "true" ]; then
        live="$(live_revision)"
    fi

    say ""
    box_top
    box_row "${C_BOLD}${C_WHITE}${GLYPH_DIAMOND}  WG-Guard Bot${C_RESET} ${C_DIM}· control menu${C_RESET}"
    box_row "${C_DIM}Telegram VPN shop for WG-Guard panels${C_RESET}"
    box_row ""
    if [ "$installed" = "true" ]; then
        box_row "$(service_badge wgguard-bot bot)    $(service_badge wgguard-db database)    $(service_badge wgguard-redis redis)    $(service_badge wgguard-caddy caddy)"
        box_row "PANEL     ${C_CYAN}$(panel_url)${C_RESET}"
        running_rev="${live:-unknown}"
        if [ "$running_rev" = "unknown" ] || [ -z "$checkout" ] || [ "$running_rev" = "$checkout" ]; then
            box_row "REVISION  ${checkout:-unknown}  ${C_DIM}(checked out)${C_RESET}"
        else
            box_row "REVISION  ${C_YELLOW}${running_rev} running · ${checkout} checked out${C_RESET}"
        fi
    else
        box_row "${C_YELLOW}Not installed yet${C_RESET} — choose 1 to set everything up."
    fi
    if [ -d "$INSTALL_DIR" ]; then
        box_row "${C_DIM}${INSTALL_DIR}${C_RESET}"
    else
        box_row "${C_DIM}${INSTALL_DIR} (will be created)${C_RESET}"
    fi
    box_bottom

    if ! docker_present; then
        printf '\n%s%s  Docker is not installed on this server — item 1 installs it.%s\n' \
            "$C_YELLOW" "$GLYPH_WARN" "$C_RESET"
    fi

    rule "Setup"
    menu_item 1 "Install / reinstall"
    menu_item 2 "Update to the latest version"    "pull, rebuild, migrate"

    rule "Operations"
    menu_item 3 "Status and health"               "services, panel, revision, disk"
    menu_item 4 "Live logs"                       "bot · database · redis · caddy · all"
    menu_item 5 "Back up the database and .env"
    menu_item 6 "Restore a backup"

    rule "Configuration"
    menu_item 7 "Domain and SSL"                  "status · set · renew · disable"
    menu_item 8 "Panel password"                  "reset the owner login"
    menu_item 9 "Rewrite .env in English"         "keeps every value"
    menu_item 10 "Maintenance"                    "migrate · restart · prune · shell"

    rule "Danger"
    menu_item 11 "Uninstall"                      "stop only, or delete everything"

    printf '\n   %2s   %s\n' "0" "Exit"
    printf '%s   %s  bash menu.sh status | logs bot | backup | restore%s\n' \
        "$C_DIM" "Tip: no menu needed —" "$C_RESET"
}

main_loop() {
    trap ':' INT          # Ctrl-C stops a log stream; 'q' leaves the menu
    local choice
    while :; do
        render_home
        if ! prompt_read choice; then
            say ""
            break
        fi
        case "$choice" in
            1)  act_install || warn "The installer did not finish."; detect_dir; pause ;;
            2)  act_update || warn "The update did not finish."; pause ;;
            3)  act_status || true; pause ;;
            4)  require_docker && menu_logs ;;
            5)  act_backup || warn "The backup did not finish."; pause ;;
            6)  act_restore || warn "The restore did not finish."; pause ;;
            7)  require_docker; act_domain || warn "The domain action did not finish."; pause ;;
            8)  act_password || warn "Resetting the password did not finish."; pause ;;
            9)  act_env_english || warn "Rewriting .env did not finish."; pause ;;
            10) require_docker && act_maintenance; pause ;;
            11) require_docker; act_uninstall || warn "The uninstall did not finish."; pause ;;
            0|q|Q|exit|quit)
                say ""
                dim "  Bye — run 'bash menu.sh' whenever you need this menu again."
                say ""
                break
                ;;
            "") : ;;
            *)  warn "Unknown choice: $choice" ;;
        esac
    done
}

# ===========================================================================
#  Command line
# ===========================================================================
usage() {
    cat <<EOF
${C_BOLD}WG-Guard Bot — control menu${C_RESET}

${C_BOLD}Usage:${C_RESET}
  ${C_CYAN}bash menu.sh${C_RESET}                      open the interactive menu
  ${C_CYAN}bash menu.sh <command> [options]${C_RESET}  run one action and exit

${C_BOLD}Where the project is:${C_RESET}
  ${C_CYAN}--dir${C_RESET}, the current directory, this script's directory, then
  ${C_DIM}${HOME:-/root}/wg-guard-bot${C_RESET} — and ${C_DIM}/root/wg-guard-bot${C_RESET} or
  ${C_DIM}/opt/wg-guard-bot${C_RESET} as a last resort.  The menu can therefore be run from
  anywhere, including straight from the README's one-liner, and still find the
  installation it is managing.

${C_BOLD}Commands:${C_RESET}
  ${C_CYAN}install${C_RESET}                         install or reconfigure (runs install.sh)
  ${C_CYAN}update${C_RESET}                          pull, rebuild, migrate (runs update.sh)
  ${C_CYAN}status${C_RESET}                          services, panel health, revision, disk
  ${C_CYAN}logs <service>${C_RESET}                  bot | db | redis | caddy | all
  ${C_CYAN}backup${C_RESET}                          dump the database and .env into ./backups
  ${C_CYAN}restore [file]${C_RESET}                  restore a dump (asks before replacing data)
  ${C_CYAN}domain${C_RESET}                          show the domain and certificate status
  ${C_CYAN}password${C_RESET}                        reset the panel owner password
  ${C_CYAN}env-english${C_RESET}                     rewrite .env with English comments, values kept
  ${C_CYAN}maintenance <action>${C_RESET}            migrate | restart | rebuild | prune | images | shell | psql
  ${C_CYAN}uninstall${C_RESET}                       stop the containers (the data stays)
  ${C_CYAN}purge${C_RESET}                           remove the containers AND the data

${C_BOLD}Options:${C_RESET}
  ${C_CYAN}--dir PATH${C_RESET}      project directory (default: found automatically, see above)
  ${C_CYAN}--tail N${C_RESET}        lines of log history to load (default: ${LOG_TAIL})
  ${C_CYAN}--no-follow${C_RESET}     print the log once instead of streaming it
  ${C_CYAN}--yes${C_RESET}           answer yes to every confirmation
  ${C_CYAN}--ascii${C_RESET}         use ASCII glyphs instead of box-drawing characters
  ${C_CYAN}-h, --help${C_RESET}      show this help

${C_DIM}Everything here is English on purpose: a Linux terminal has no bidi support and
renders Persian backwards.  Bot copy and the panel UI stay Persian.${C_RESET}
EOF
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --dir)       INSTALL_DIR="${2:-}"; shift ;;
            --dir=*)     INSTALL_DIR="${1#*=}" ;;
            --tail)      LOG_TAIL="${2:-$LOG_TAIL}"; shift ;;
            --tail=*)    LOG_TAIL="${1#*=}" ;;
            --no-follow|--snapshot) COMMAND_ARGS+=("--no-follow") ;;
            --yes|-y)    ASSUME_YES="true" ;;
            --ascii)     ASCII="true" ;;
            --help|-h)   COMMAND="help" ;;
            --*)         err "Unknown option: $1"; say ""; usage; exit 2 ;;
            *)
                if [ -z "$COMMAND" ]; then
                    COMMAND="$1"
                else
                    COMMAND_ARGS+=("$1")
                fi
                ;;
        esac
        shift
    done
}

run_command() {
    local cmd="$1"
    shift
    case "$cmd" in
        install)          act_install "$@" ;;
        update)           act_update ;;
        status)           act_status ;;
        logs)             act_logs "$@" ;;
        backup)           act_backup ;;
        restore)          act_restore "$@" ;;
        domain)           act_domain "$@" ;;
        password|passwd)  act_password "$@" ;;
        env-english|env)  act_env_english ;;
        maintenance)      act_maintenance "$@" ;;
        uninstall)        act_uninstall stop ;;
        purge)            act_uninstall purge ;;
        help)             usage ;;
        *)                err "Unknown command: $cmd"; say ""; usage; exit 2 ;;
    esac
}

main() {
    parse_args "$@"
    init_colors
    detect_dir
    ensure_utf8_locale
    if [ "$ASCII" = "true" ]; then
        use_ascii_glyphs
    fi

    case "$COMMAND" in
        help)    usage; return 0 ;;
        version) printf '%s\n' "$(project_version)"; return 0 ;;
        "")      : ;;   # no command: interactive mode
        install|update|status|logs|backup|restore|domain|password|passwd|env-english|env|maintenance|uninstall|purge) : ;;
        *)       err "Unknown command: $COMMAND"; say ""; usage; exit 2 ;;
    esac

    require_root "$@"
    PROJECT_VERSION="$(project_version)"

    if [ -n "$COMMAND" ]; then
        if [ "$COMMAND" != "install" ] && ! is_installed; then
            require_installed
            return 1
        fi
        run_command "$COMMAND" ${COMMAND_ARGS[@]+"${COMMAND_ARGS[@]}"}
        return $?
    fi

    if [ ! -t 0 ]; then
        usage
        return 0
    fi

    main_loop
    return 0
}

main "$@"
