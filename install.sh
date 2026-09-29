#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — automatic installer for fresh Ubuntu/Debian servers
#  WG-Guard Bot — one-command installer for a fresh Ubuntu/Debian VPS.
#
#  Quick start:
#      bash <(curl -fsSL https://raw.githubusercontent.com/Sir-Adnan/wg-guard-bot/main/install.sh)
#
#  This script installs Docker (if missing), writes .env with generated keys and
#  random passwords, asks for the bot details, starts the services and
#  waits until the panel is ready.
# ---------------------------------------------------------------------------
set -euo pipefail

# ===========================================================================
#  Defaults
# ===========================================================================
REPO_URL="${WGGB_REPO_URL:-https://github.com/Sir-Adnan/wg-guard-bot.git}"
BRANCH="main"
INSTALL_DIR=""
ASSUME_YES="false"
FORCE="false"
SKIP_DOCKER="false"
SHOW_HELP="false"

OPT_BOT_TOKEN=""
OPT_ADMIN_IDS=""
OPT_SUPPORT_IDS=""
OPT_PORT=""
OPT_PANEL_URL=""
OPT_OWNER_USERNAME=""
OPT_OWNER_PASSWORD=""
OPT_APP_NAME=""
OPT_PUBLIC_IP=""
OPT_DOMAIN=""
OPT_ACME_EMAIL=""
NO_DOMAIN="false"

PANEL_PORT_DEFAULT="8080"
OWNER_USERNAME_DEFAULT="admin"
APP_NAME_DEFAULT="فروشگاه VPN"
HEALTH_TIMEOUT_DEFAULT="120"
# issuance of the first certificate needs the DNS to be live; give it more room
HTTPS_TIMEOUT_DEFAULT="180"
PANEL_BIND_DEFAULT="0.0.0.0"
TLS_BIND="127.0.0.1"

# Shared values filled in during the run (declared up front so set -u is happy)
COMPOSE=""
PUBLIC_IP=""
INSTALL_DIR=""
EXISTING="false"
REUSE_ENV="false"
ENV_BACKUP=""
ALLOW_UFW_HINT="false"
OWNER_USERNAME_SHOWN=""
OWNER_PASSWORD_SHOWN=""
BOT_TOKEN_FINAL=""
ADMIN_IDS_FINAL=""
PANEL_PORT_FINAL=""
PANEL_URL_FINAL=""
TLS_ENABLED="false"
COMPOSE_PROFILE=""
DOMAIN_FINAL=""
ACME_EMAIL_FINAL=""
PANEL_BIND_FINAL="0.0.0.0"
CERT_ISSUED="false"
CERT_DETAILS=""
OLD_SECRET_KEY=""; OLD_POSTGRES_PASSWORD=""; OLD_WEBHOOK_SECRET=""
OLD_OWNER_USERNAME=""; OLD_OWNER_PASSWORD=""; OLD_BOT_TOKEN=""
OLD_ADMIN_IDS=""; OLD_SUPPORT_IDS=""; OLD_PANEL_PORT=""
OLD_PANEL_URL=""; OLD_APP_NAME=""; OLD_BACKUP_INTERVAL=""
OLD_DOMAIN=""; OLD_ACME_EMAIL=""; OLD_PANEL_BIND=""
BACKUP_HOURS_SHOWN="24"

# ===========================================================================
#  Colours — only when stdout is a terminal and NO_COLOR is not set
# ===========================================================================
if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
    USE_COLOR="false"
else
    USE_COLOR="true"
fi

if [ "$USE_COLOR" = "true" ]; then
    C_RESET=$'\033[0m'; C_BOLD=$'\033[1m'; C_DIM=$'\033[2m'
    C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_BLUE=$'\033[34m'; C_MAGENTA=$'\033[35m'; C_CYAN=$'\033[36m'
else
    C_RESET=""; C_BOLD=""; C_DIM=""
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_MAGENTA=""; C_CYAN=""
fi

# ===========================================================================
#  Output helpers
# ===========================================================================
say()  { printf '%s\n' "$*"; }
info() { printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
ok()   { printf '%s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s⚠%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s✖%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
step() { printf '\n%s%s▸ %s%s\n' "$C_BOLD" "$C_BLUE" "$*" "$C_RESET"; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

# ---------------------------------------------------------------------------
#  Version — read from pyproject.toml so it can never drift from the
#  real version. If the file is not available
#  (running straight from curl), no version is printed.
# ---------------------------------------------------------------------------
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd -P || true)"
APP_VERSION=""

read_app_version() {
    local file version
    for file in "${SCRIPT_DIR:+$SCRIPT_DIR/pyproject.toml}" "./pyproject.toml"; do
        [ -f "$file" ] || continue
        version="$(sed -n 's/^version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$file" | head -n 1)"
        if [ -n "$version" ]; then
            APP_VERSION="$version"
            return 0
        fi
    done
    return 0
}

banner() {
    printf '%s' "$C_MAGENTA"
    cat <<'ASCII'
   ╦ ╦╔═╗  ╔═╗╦ ╦╔═╗╦═╗╔╦╗     ╔╗ ╔═╗╔╦╗
   ║║║║ ╦  ║ ╦║ ║╠═╣╠╦╝ ║║ ─── ╠╩╗║ ║ ║
   ╚╩╝╚═╝  ╚═╝╚═╝╩ ╩╩╚══╩╝     ╚═╝╚═╝ ╩
ASCII
    printf '%s' "$C_RESET"
    if [ -n "$APP_VERSION" ]; then
        printf '%s\n' "   Automatic installer — version ${C_BOLD}${APP_VERSION}${C_RESET}"
    fi
    printf '%s\n\n' "   ${C_DIM}VPN shop bot on the WG-Guard / AmneziaWG panel${C_RESET}"
}

# ===========================================================================
#  Help
# ===========================================================================
usage() {
    cat <<EOF
${C_BOLD}WG-Guard Bot — installation guide${C_RESET}

${C_BOLD}Quick start (recommended):${C_RESET}
  bash <(curl -fsSL https://raw.githubusercontent.com/Sir-Adnan/wg-guard-bot/main/install.sh)

${C_BOLD}Manual install:${C_RESET}
  git clone https://github.com/Sir-Adnan/wg-guard-bot.git wg-guard-bot
  cd wg-guard-bot && bash install.sh

${C_BOLD}Command-line options:${C_RESET}
  ${C_CYAN}--yes${C_RESET}                 non-interactive mode; every value comes from a flag or its default
                          (in this mode ${C_BOLD}--bot-token${C_RESET} and ${C_BOLD}--admin-ids${C_RESET} are required)
  ${C_CYAN}--bot-token TOKEN${C_RESET}     bot token from @BotFather
  ${C_CYAN}--admin-ids IDS${C_RESET}       numeric admin IDs, comma-separated: 111,222
  ${C_CYAN}--support-ids IDS${C_RESET}     numeric support IDs (optional, comma-separated)
  ${C_CYAN}--port PORT${C_RESET}           admin panel port (default: ${PANEL_PORT_DEFAULT})
  ${C_CYAN}--panel-url URL${C_RESET}       public panel URL (default: http://IP:PORT)
  ${C_CYAN}--owner-username NAME${C_RESET} panel owner username (default: ${OWNER_USERNAME_DEFAULT})
  ${C_CYAN}--owner-password PASS${C_RESET} panel owner password (default: generated, readable)
  ${C_CYAN}--app-name NAME${C_RESET}       shop display name as customers see it (default: a Persian name)
  ${C_CYAN}--domain NAME${C_RESET}         domain or subdomain for automatic SSL, e.g. bot.example.com
                          (a domain enables https and webhook mode automatically)
  ${C_CYAN}--acme-email MAIL${C_RESET}     Let's Encrypt email for certificate expiry warnings (optional)
  ${C_CYAN}--no-domain${C_RESET}           no domain and no SSL (polling mode on http://IP:PORT)
  ${C_CYAN}--ip ADDRESS${C_RESET}          set the server public IP manually (default: auto-detect)
  ${C_CYAN}--dir PATH${C_RESET}            install/repository path (default: current directory or ~/wg-guard-bot)
  ${C_CYAN}--branch NAME${C_RESET}         repository branch to install and update from (default: main)
  ${C_CYAN}--force${C_RESET}               overwrite an existing .env (a backup is kept) even in --yes mode
  ${C_CYAN}--no-docker-install${C_RESET}   if Docker is missing, do not install it (needs root access)
  ${C_CYAN}-h, --help${C_RESET}            show this help

${C_BOLD}Fully unattended install example:${C_RESET}
  bash install.sh --yes \\
      --bot-token 123456789:AA... \\
      --admin-ids 111111111 \\
      --port 8080

${C_BOLD}Notes:${C_RESET}
  • This script needs root access (it re-runs itself with sudo if it is not).
  • Running the script again is safe; the existing .env is backed up and
    existing passwords are left untouched.

${C_DIM}One-command installer for WG-Guard Bot (Docker Compose stack) on a
fresh Ubuntu/Debian VPS — generates .env, builds the image, starts the stack
and waits for the panel health endpoint.${C_RESET}
EOF
}

# ===========================================================================
#  Argument parsing
# ===========================================================================
parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --yes|-y)             ASSUME_YES="true" ;;
            --force|-f)           FORCE="true" ;;
            --no-docker-install)  SKIP_DOCKER="true" ;;
            --help|-h)            SHOW_HELP="true" ;;
            --bot-token)          OPT_BOT_TOKEN="${2:-}"; shift ;;
            --admin-ids)          OPT_ADMIN_IDS="${2:-}"; shift ;;
            --support-ids)        OPT_SUPPORT_IDS="${2:-}"; shift ;;
            --port)               OPT_PORT="${2:-}"; shift ;;
            --panel-url)          OPT_PANEL_URL="${2:-}"; shift ;;
            --owner-username)     OPT_OWNER_USERNAME="${2:-}"; shift ;;
            --owner-password)     OPT_OWNER_PASSWORD="${2:-}"; shift ;;
            --app-name)           OPT_APP_NAME="${2:-}"; shift ;;
            --dir)                INSTALL_DIR="${2:-}"; shift ;;
            --branch)             BRANCH="${2:-}"; shift ;;
            --ip)                 OPT_PUBLIC_IP="${2:-}"; shift ;;
            --domain)             OPT_DOMAIN="${2:-}"; shift ;;
            --acme-email)         OPT_ACME_EMAIL="${2:-}"; shift ;;
            --no-domain)          NO_DOMAIN="true" ;;
            --bot-token=*)        OPT_BOT_TOKEN="${1#*=}" ;;
            --admin-ids=*)        OPT_ADMIN_IDS="${1#*=}" ;;
            --support-ids=*)      OPT_SUPPORT_IDS="${1#*=}" ;;
            --port=*)             OPT_PORT="${1#*=}" ;;
            --panel-url=*)        OPT_PANEL_URL="${1#*=}" ;;
            --owner-username=*)   OPT_OWNER_USERNAME="${1#*=}" ;;
            --owner-password=*)   OPT_OWNER_PASSWORD="${1#*=}" ;;
            --app-name=*)         OPT_APP_NAME="${1#*=}" ;;
            --dir=*)              INSTALL_DIR="${1#*=}" ;;
            --branch=*)           BRANCH="${1#*=}" ;;
            --ip=*)               OPT_PUBLIC_IP="${1#*=}" ;;
            --domain=*)           OPT_DOMAIN="${1#*=}" ;;
            --acme-email=*)       OPT_ACME_EMAIL="${1#*=}" ;;
            -*)
                err "unknown option: $1"
                say ""
                dim "run this to see the help: bash install.sh --help"
                exit 2
                ;;
            *)
                err "unexpected extra argument: $1"
                exit 2
                ;;
        esac
        shift
    done
}

# ===========================================================================
#  Preflight checks
# ===========================================================================
require_root() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        warn "This script needs root access; re-running it with sudo now…"
        exec sudo -E bash "$0" "$@"
    fi
    err "Installation needs root access and sudo is not installed either."
    say "  Log in as root, or install sudo: apt-get install -y sudo"
    exit 1
}

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
        return 0
    fi
    # Compose v1 (docker-compose) is end-of-life and lacks the flags this
    # script relies on (--profile, ps --status), so refuse it explicitly
    # instead of degrading to a silent timeout.
    if command -v docker-compose >/dev/null 2>&1; then
        warn "The old docker-compose (v1) is installed; this script needs Docker Compose v2."
        say "  Install it: apt-get update && apt-get install -y docker-compose-plugin"
    fi
    COMPOSE=""
    return 1
}

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        return 0
    fi
    if [ "$SKIP_DOCKER" = "true" ]; then
        err "Docker is not installed and --no-docker-install was passed."
        say "  Either install Docker yourself, or run the script without that flag."
        exit 1
    fi
    step "Docker is not installed; installing it from the official script…"
    dim "  (takes a few minutes and needs internet access)"
    if ! command -v curl >/dev/null 2>&1; then
        warn "curl is not installed; installing it now…"
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq >/dev/null 2>&1 || true
            apt-get install -y -qq curl >/dev/null 2>&1 || true
        fi
    fi
    if ! command -v curl >/dev/null 2>&1; then
        err "curl is not installed and installing it automatically failed."
        say "  Install it manually: apt-get install -y curl"
        exit 1
    fi
    if ! curl -fsSL https://get.docker.com -o /tmp/get-docker.sh; then
        err "Downloading the Docker install script failed."
        say "  Check the server's internet connection and try again."
        exit 1
    fi
    if ! sh /tmp/get-docker.sh; then
        err "Installing Docker failed (the official script exited with an error)."
        say "  You can install it manually: https://docs.docker.com/engine/install/"
        rm -f /tmp/get-docker.sh
        exit 1
    fi
    rm -f /tmp/get-docker.sh
    ok "Docker installed successfully."
    if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
        warn "Docker is installed but the compose plugin is missing; trying to install it…"
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq >/dev/null 2>&1 || true
            apt-get install -y -qq docker-compose-plugin >/dev/null 2>&1 || \
                apt-get install -y -qq docker-compose >/dev/null 2>&1 || true
        fi
    fi
}

detect_public_ip() {
    PUBLIC_IP="${OPT_PUBLIC_IP:-}"
    if [ -z "$PUBLIC_IP" ] && command -v curl >/dev/null 2>&1; then
        PUBLIC_IP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
        if [ -z "$PUBLIC_IP" ]; then
            PUBLIC_IP="$(curl -fsS --max-time 6 https://ifconfig.me/ip 2>/dev/null || true)"
        fi
    fi
    if [ -z "$PUBLIC_IP" ] && command -v hostname >/dev/null 2>&1; then
        PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    fi
    if [ -z "$PUBLIC_IP" ]; then
        PUBLIC_IP="SERVER_IP"
    fi
    return 0
}

# ===========================================================================
#  Interactive prompts
# ===========================================================================
# ask <label> <default> [hint]
ask() {
    local label="$1" default="$2" hint="${3:-}" answer=""
    printf '%s? %s%s' "$C_BOLD" "$label" "$C_RESET"
    if [ -n "$default" ]; then
        printf ' %s[%s]%s' "$C_DIM" "$default" "$C_RESET"
    fi
    if [ -n "$hint" ]; then
        printf '\n  %s%s%s\n' "$C_DIM" "$hint" "$C_RESET"
    else
        printf '\n'
    fi
    printf '  %s❯%s ' "$C_GREEN" "$C_RESET"
    if ! IFS= read -r answer; then
        answer=""
    fi
    if [ -z "$answer" ]; then
        answer="$default"
    fi
    printf '%s' "$answer"
}

# ask_required <label> <default> <hint> <validation pattern> <error message>
ask_required() {
    local label="$1" default="$2" hint="$3" pattern="$4" errmsg="$5" answer="" value=""
    while :; do
        answer="$(ask "$label" "$default" "$hint")"
        value="$(printf '%s' "$answer" | tr -d '[:space:]')"
        if [ -z "$value" ]; then
            err "$errmsg"
            continue
        fi
        if [ -n "$pattern" ] && ! printf '%s' "$value" | grep -Eq "$pattern"; then
            err "$errmsg"
            continue
        fi
        printf '%s' "$value"
        return 0
    done
}

confirm() {
    # confirm <question> [default y/n]
    local question="$1" default="${2:-n}" answer=""
    if [ "${ASSUME_YES:-false}" = "true" ]; then
        [ "$default" = "y" ]
        return $?
    fi
    printf '%s? %s (y/n)%s %s[%s]%s\n  %s❯%s ' \
        "$C_BOLD" "$question" "$C_RESET" "$C_DIM" "$default" "$C_RESET" "$C_GREEN" "$C_RESET" >&2
    IFS= read -r answer || answer=""
    [ -n "$answer" ] || answer="$default"
    case "$answer" in
        y|Y|yes|YES|بله|ب) return 0 ;;
        *) return 1 ;;
    esac
}

# ===========================================================================
#  Secret generation
# ===========================================================================
gen_hex() {
    # 64 hex characters for SECRET_KEY
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return 0
    fi
    head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-64
}

gen_password() {
    # 24-character password for POSTGRES_PASSWORD (no characters that break .env or psql)
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 24 | tr -d '/+=' | cut -c1-24
        return 0
    fi
    head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24
}

gen_readable_password() {
    # 16-character readable password for the panel owner — no confusing characters (0/O, 1/l/I)
    local alphabet="ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    local length="16" out="" index="" hex=""
    if command -v openssl >/dev/null 2>&1; then
        while [ "${#out}" -lt "$length" ]; do
            hex="$(openssl rand -hex 2 | tr -dc '0-9a-f')"
            [ "${#hex}" -ge 4 ] || continue
            index=$(( 16#${hex:0:4} % 57 ))
            out="${out}${alphabet:${index}:1}"
        done
        printf '%s' "$out"
        return 0
    fi
    local i=0
    while [ "$i" -lt "$length" ]; do
        index=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % 57 ))
        out="${out}${alphabet:${index}:1}"
        i=$(( i + 1 ))
    done
    printf '%s' "$out"
}

gen_webhook_secret() {
    # secret webhook path — letters and digits only (used in a URL)
    local out=""
    if command -v openssl >/dev/null 2>&1; then
        out="$(openssl rand -hex 16)"
    else
        out="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32)"
    fi
    printf '%s' "$out"
}

# ===========================================================================
#  Read an existing value from .env
# ===========================================================================
env_get() {
    # env_get <file> <key> — prints the value without quotes
    local file="$1" key="$2" line=""
    [ -f "$file" ] || return 0
    line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n1 || true)"
    [ -n "$line" ] || return 0
    line="${line#*=}"
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    # strip leading and trailing quotes
    case "$line" in
        \"*\") line="${line#\"}"; line="${line%\"}" ;;
        \'*\') line="${line#\'}"; line="${line%\'}" ;;
    esac
    printf '%s' "$line"
}

env_set() {
    # env_set <file> <key> <value> — replaces the whole line or appends it
    local file="$1" key="$2" value="$3"
    if grep -Eq "^[[:space:]]*${key}=" "$file"; then
        # use | as the delimiter and escape the characters sed treats specially
        local escaped
        escaped="$(printf '%s' "$value" | sed -e 's/[&\\]/\\&/g')"
        sed -i "s|^[[:space:]]*${key}=.*|${key}=${escaped}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
}

validate_port() {
    case "$1" in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

validate_ids() {
    # digits and commas only; at least one ID
    printf '%s' "$1" | grep -Eq '^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$'
}

# ===========================================================================
#  Domain and automatic TLS (Caddy)
# ===========================================================================
normalize_domain() {
    # strip the scheme (http/https), the path and any trailing slash; lowercase it
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's|^[[:space:]]*||' -e 's|[[:space:]]*$||' \
              -e 's|^[a-z][a-z0-9+.-]*://||' \
              -e 's|/.*$||' \
              -e 's|^\.*||' -e 's|\.*$||'
}

validate_domain() {
    # a valid domain (at least two labels), not an IP and not localhost
    printf '%s' "$1" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
}

is_ip_address() {
    # plain IPv4, or anything made only of digits and dots
    printf '%s' "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
}

is_private_ip() {
    # private/reserved addresses — useless for a DNS comparison, and a sign of
    # DNS hijacking or a local resolver rather than a real A record.
    local ip="$1" a b
    case "$ip" in
        10.*|127.*|169.254.*|192.168.*|0.*) return 0 ;;
    esac
    a="$(printf '%s' "$ip" | cut -d. -f1)"
    b="$(printf '%s' "$ip" | cut -d. -f2)"
    if [ "$a" = "172" ] && [ "$b" -ge 16 ] 2>/dev/null && [ "$b" -le 31 ] 2>/dev/null; then
        return 0
    fi
    # RFC 5737 documentation ranges
    case "$ip" in
        192.0.2.*|198.51.100.*|203.0.113.*) return 0 ;;
    esac
    return 1
}

is_email() {
    printf '%s' "$1" | grep -Eq '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'
}

resolve_domain_ips() {
    # print the public IPs the domain resolves to (one per line)
    # private/reserved addresses are filtered out so a local resolver or DNS hijack
    # does not trigger a false "the domain points here" warning.
    local name="$1" ips="" ip=""
    if command -v getent >/dev/null 2>&1; then
        ips="$(getent ahostsv4 "$name" 2>/dev/null | awk '{print $1}' | sort -u || true)"
    fi
    if [ -z "$ips" ] && command -v dig >/dev/null 2>&1; then
        ips="$(dig +short A "$name" 2>/dev/null | grep -E '^[0-9.]+$' | sort -u || true)"
    fi
    if [ -z "$ips" ] && command -v host >/dev/null 2>&1; then
        ips="$(host -t A "$name" 2>/dev/null | awk '/has address/ {print $NF}' | sort -u || true)"
    fi
    if [ -z "$ips" ] && command -v nslookup >/dev/null 2>&1; then
        ips="$(nslookup "$name" 2>/dev/null | awk '/^Address: / {print $2}' | grep -E '^[0-9.]+$' | sort -u || true)"
    fi
    if [ -z "$ips" ] && command -v python3 >/dev/null 2>&1; then
        ips="$(python3 -c 'import socket,sys
try:
    print("\n".join(sorted({i[4][0] for i in socket.getaddrinfo(sys.argv[1], None, socket.AF_INET)})))
except Exception:
    pass' "$name" 2>/dev/null || true)"
    fi
    for ip in $ips; do
        if ! is_private_ip "$ip"; then
            printf '%s\n' "$ip"
        fi
    done
}

check_dns_points_here() {
    # 0 = the domain points to this server, 1 = it does not or did not resolve
    local name="$1" ips="" ip=""
    ips="$(resolve_domain_ips "$name")"
    RESOLVED_IPS="$(printf '%s' "$ips" | tr '\n' ' ' | sed -e 's/[[:space:]]*$//')"
    [ -n "$ips" ] || return 1
    [ "$PUBLIC_IP" = "SERVER_IP" ] && return 1
    for ip in $ips; do
        if [ "$ip" = "$PUBLIC_IP" ]; then
            return 0
        fi
    done
    return 1
}

show_dns_warning() {
    warn "The domain '${DOMAIN_FINAL}' does not point to this server yet."
    say "   Server IP        : ${PUBLIC_IP}"
    say "   Domain IP in DNS : ${RESOLVED_IPS:-not found}"
    say ""
    dim "  To get an SSL certificate, create an A record from the domain to the server IP:"
    dim "      ${DOMAIN_FINAL}  A  ${PUBLIC_IP}"
    dim "  (DNS propagation usually takes a few minutes)"
    say ""
}

cert_status() {
    # read the certificate from the caddy_data volume and print a summary
    local domain="$1" out=""
    out="$($COMPOSE_PROFILE exec -T caddy sh -c "
        f=\$(find /data/caddy/certificates -name '${domain}.crt' 2>/dev/null | head -n1)
        [ -n \"\$f\" ] || exit 1
        openssl x509 -in \"\$f\" -noout -subject -issuer -enddate 2>/dev/null
    " 2>/dev/null || true)"
    [ -n "$out" ] || return 1
    CERT_DETAILS="$(printf '%s' "$out" | tr '\n' ' | ' | sed -e 's/[[:space:]]*| *$//')"
    return 0
}

# ===========================================================================
#  Project directory
# ===========================================================================
prepare_dir() {
    if [ -z "$INSTALL_DIR" ]; then
        if [ -f "./docker-compose.yml" ]; then
            INSTALL_DIR="$(pwd)"
        else
            INSTALL_DIR="${HOME:-/root}/wg-guard-bot"
        fi
    fi
    INSTALL_DIR="${INSTALL_DIR%/}"

    if [ -f "$INSTALL_DIR/docker-compose.yml" ]; then
        ok "Project directory found: $INSTALL_DIR"
    else
        step "Cloning the project into $INSTALL_DIR"
        if [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
            err "The directory '$INSTALL_DIR' is not empty and has no docker-compose.yml either."
            say "  Pick an empty path (--dir), or clone the project manually:"
            dim "    git clone --branch $BRANCH ${REPO_URL%.git}.git \"$INSTALL_DIR\""
            exit 1
        fi
        if ! command -v git >/dev/null 2>&1; then
            err "git is required to fetch the code and it is not installed."
            say "  Install it: apt-get update && apt-get install -y git"
            exit 1
        fi
        if ! git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"; then
            err "Cloning the repository failed: $REPO_URL"
            say "  If your repository lives elsewhere, pass the right URL:"
            dim "    WGGB_REPO_URL=https://github.com/Sir-Adnan/wg-guard-bot.git bash install.sh --branch $BRANCH"
            say "  Or clone the project manually and run install.sh from inside that directory."
            exit 1
        fi
        ok "Project code downloaded."
    fi

    cd "$INSTALL_DIR"
    if [ ! -f "./.env.example" ]; then
        err ".env.example was not found in '$INSTALL_DIR'; this does not look like the right directory."
        exit 1
    fi
}

# ===========================================================================
#  Detect previous .env values / reuse an existing installation
# ===========================================================================
load_existing() {
    # previous values; all declared up front so set -u is happy
    EXISTING="false"
    OLD_SECRET_KEY=""; OLD_POSTGRES_PASSWORD=""; OLD_WEBHOOK_SECRET=""
    OLD_OWNER_USERNAME=""; OLD_OWNER_PASSWORD=""; OLD_BOT_TOKEN=""
    OLD_ADMIN_IDS=""; OLD_SUPPORT_IDS=""; OLD_PANEL_PORT=""
    OLD_PANEL_URL=""; OLD_APP_NAME=""; OLD_BACKUP_INTERVAL=""
    OLD_DOMAIN=""; OLD_ACME_EMAIL=""; OLD_PANEL_BIND=""

    [ -f ./.env ] || return 0
    EXISTING="true"
    OLD_SECRET_KEY="$(env_get .env SECRET_KEY)"
    OLD_POSTGRES_PASSWORD="$(env_get .env POSTGRES_PASSWORD)"
    OLD_WEBHOOK_SECRET="$(env_get .env WEBHOOK_SECRET)"
    OLD_OWNER_USERNAME="$(env_get .env OWNER_USERNAME)"
    OLD_OWNER_PASSWORD="$(env_get .env OWNER_PASSWORD)"
    OLD_BOT_TOKEN="$(env_get .env BOT_TOKEN)"
    OLD_ADMIN_IDS="$(env_get .env ADMIN_IDS)"
    OLD_SUPPORT_IDS="$(env_get .env SUPPORT_IDS)"
    OLD_PANEL_PORT="$(env_get .env PANEL_PORT)"
    OLD_PANEL_URL="$(env_get .env PANEL_BASE_URL)"
    OLD_APP_NAME="$(env_get .env APP_NAME)"
    OLD_BACKUP_INTERVAL="$(env_get .env BACKUP_INTERVAL_HOURS)"
    OLD_DOMAIN="$(env_get .env DOMAIN)"
    OLD_ACME_EMAIL="$(env_get .env ACME_EMAIL)"
    OLD_PANEL_BIND="$(env_get .env PANEL_BIND)"
    return 0
}

handle_existing_env() {
    [ "$EXISTING" = "true" ] || return 0

    say ""
    warn "An .env file from a previous install was found."
    dim "  Existing passwords and keys are kept as they are, so the database and panel keep working."

    if [ "$ASSUME_YES" != "true" ]; then
        if ! confirm "Rebuild .env with the new settings? (a backup is taken)" "n"; then
            REUSE_ENV="true"
            ok "Using the existing .env."
            return 0
        fi
    else
        if [ "$FORCE" != "true" ]; then
            REUSE_ENV="true"
            ok "--yes mode: the existing .env was kept (pass --force to overwrite)."
            return 0
        fi
        warn "--force is set; .env will be rewritten (the old passwords are kept)."
    fi

    ENV_BACKUP=".env.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p .env "$ENV_BACKUP"
    ok "Backup created: $ENV_BACKUP"
}

# ===========================================================================
#  Build the .env file
# ===========================================================================
write_env() {
    local target secret_key pg_password webhook_secret
    local bot_token admin_ids support_ids panel_port panel_url
    local owner_username owner_password app_name app_name_default secret_len
    local domain_input acme_input panel_bind domain_error
    local DOMAIN_FROM_PROMPT=""
    local DOMAIN_VALUE="" ACME_EMAIL_VALUE="" PANEL_BIND_VALUE=""

    step "Writing the .env configuration file"

    # -- secrets: on an existing install we keep the current ones -------------
    secret_key="${OLD_SECRET_KEY:-}"
    pg_password="${OLD_POSTGRES_PASSWORD:-}"
    webhook_secret="${OLD_WEBHOOK_SECRET:-}"

    [ -n "$secret_key" ] || secret_key="$(gen_hex)"
    [ -n "$pg_password" ] || pg_password="$(gen_password)"
    [ -n "$webhook_secret" ] || webhook_secret="$(gen_webhook_secret)"

    # -- interactive prompts ----------------------------------------------------
    bot_token="$OPT_BOT_TOKEN"
    admin_ids="$OPT_ADMIN_IDS"
    support_ids="$OPT_SUPPORT_IDS"
    panel_port="$OPT_PORT"
    panel_url="$OPT_PANEL_URL"
    owner_username="$OPT_OWNER_USERNAME"
    owner_password="$OPT_OWNER_PASSWORD"
    app_name="$OPT_APP_NAME"

    # previous values as defaults
    [ -n "$bot_token" ]       || bot_token="${OLD_BOT_TOKEN:-}"
    [ -n "$admin_ids" ]       || admin_ids="${OLD_ADMIN_IDS:-}"
    [ -n "$support_ids" ]     || support_ids="${OLD_SUPPORT_IDS:-}"
    [ -n "$panel_port" ]      || panel_port="${OLD_PANEL_PORT:-}"
    [ -n "$panel_url" ]       || panel_url="${OLD_PANEL_URL:-}"
    [ -n "$owner_username" ]  || owner_username="${OLD_OWNER_USERNAME:-}"
    [ -n "$app_name" ]        || app_name="${OLD_APP_NAME:-}"

    if [ "$ASSUME_YES" = "true" ]; then
        # ---- non-interactive mode: strict validation, fail fast -------------------
        if [ -z "$bot_token" ]; then
            err "In --yes mode you must pass the bot token."
            say "  Example: bash install.sh --yes --bot-token 123456789:AA... --admin-ids 111111111"
            exit 2
        fi
        if ! printf '%s' "$bot_token" | grep -Eq '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'; then
            err "The bot token format is wrong. It must look like: 123456789:AAH... (from @BotFather)"
            exit 2
        fi
        if [ -z "$admin_ids" ]; then
            err "In --yes mode you must pass at least one admin ID."
            say "  Example: --admin-ids 111111111,222222222"
            exit 2
        fi
        if ! validate_ids "$admin_ids"; then
            err "Admin IDs must be numeric and comma-separated; example: 111111111,222222222"
            exit 2
        fi
        if [ -n "$support_ids" ] && ! validate_ids "$support_ids"; then
            err "Support IDs must be numeric and comma-separated."
            exit 2
        fi
        [ -n "$panel_port" ] || panel_port="$PANEL_PORT_DEFAULT"
        if ! validate_port "$panel_port"; then
            err "Invalid port: $panel_port (must be between 1 and 65535)"
            exit 2
        fi
        [ -n "$owner_username" ] || owner_username="$OWNER_USERNAME_DEFAULT"
        [ -n "$app_name" ]       || app_name="$APP_NAME_DEFAULT"
        if [ -z "$owner_password" ]; then
            owner_password="${OLD_OWNER_PASSWORD:-}"
            [ -n "$owner_password" ] || owner_password="$(gen_readable_password)"
        fi
    else
        # ---- interactive mode ---------------------------------------------------
        say ""
        info "Answer a few short questions. Wherever you see a default in [ ], just press Enter."
        say ""

        if [ -z "$bot_token" ]; then
            bot_token="$(ask_required \
                "Bot token (from @BotFather)" "" \
                "Message @BotFather on Telegram → /newbot → copy the token" \
                '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$' \
                "Invalid token. It must look like 123456789:AAH... (at least 30 characters after the colon).")"
        else
            ok "Bot token taken from the flag."
        fi

        if [ -z "$admin_ids" ]; then
            admin_ids="$(ask_required \
                "Numeric admin IDs (comma-separated)" "" \
                "Get your own ID from @userinfobot. Example: 111111111,222222222" \
                '^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$' \
                "At least one numeric ID is required (digits and commas only).")"
        else
            ok "Admin IDs taken from the flag."
        fi

        support_ids="$(ask \
            "Numeric support IDs (optional, comma-separated)" "$support_ids" \
            "Support users have limited access; leave this empty if you do not want any")"
        support_ids="$(printf '%s' "$support_ids" | tr -d '[:space:]')"
        if [ -n "$support_ids" ] && ! validate_ids "$support_ids"; then
            warn "The support IDs were invalid and have been ignored."
            support_ids=""
        fi

        panel_port="$(ask "Admin panel port" "${panel_port:-$PANEL_PORT_DEFAULT}" \
            "HTTP port for the panel; if it is taken, use another one (for example 8090)")"
        panel_port="$(printf '%s' "$panel_port" | tr -d '[:space:]')"
        while ! validate_port "$panel_port"; do
            err "Invalid port; enter a number between 1 and 65535."
            panel_port="$(ask "Admin panel port" "$PANEL_PORT_DEFAULT" "")"
            panel_port="$(printf '%s' "$panel_port" | tr -d '[:space:]')"
        done

        if [ -z "$panel_url" ]; then
            panel_url="$(ask "Public panel URL" "http://${PUBLIC_IP}:${panel_port}" \
                "If you have a domain and SSL, enter it like: https://bot.example.com")"
        fi
        panel_url="$(printf '%s' "$panel_url" | sed -e 's|[[:space:]]||g' -e 's|/*$||')"

        # -- domain and SSL (optional) -------------------------------------------
        # leaving the domain empty keeps the old behaviour: polling on http://IP:PORT
        if [ "$NO_DOMAIN" != "true" ]; then
            domain_input="$(ask "Domain or subdomain (empty = no domain, no SSL)" "" \
                "If you have a domain, first point an A record at this server IP (${PUBLIC_IP})")"
            DOMAIN_FROM_PROMPT="$(normalize_domain "$domain_input")"
        fi

        owner_username="$(ask "Panel owner username" "${owner_username:-$OWNER_USERNAME_DEFAULT}" \
            "you will sign in to the panel with this username")"
        owner_username="$(printf '%s' "$owner_username" | tr -d '[:space:]')"
        [ -n "$owner_username" ] || owner_username="$OWNER_USERNAME_DEFAULT"

        if [ -z "$owner_password" ]; then
            owner_password="${OLD_OWNER_PASSWORD:-}"
            if [ -z "$owner_password" ]; then
                owner_password="$(gen_readable_password)"
            fi
        fi

        # The built-in default is a Persian shop name, which a terminal without
        # bidi support would render reversed - so it is never echoed back as a
        # prompt default.  Pressing Enter keeps whatever this install had.
        app_name_default="${app_name:-$APP_NAME_DEFAULT}"
        app_name="$(ask "Shop display name" "" \
            "shown in the bot messages and the panel; press Enter to keep the current name")"
        [ -n "$app_name" ] || app_name="$app_name_default"
    fi

    # -- domain validation (both modes) -------------------------------------
    domain_input="${OPT_DOMAIN:-}"
    [ -n "$domain_input" ] || domain_input="$DOMAIN_FROM_PROMPT"
    DOMAIN_VALUE="$(normalize_domain "$domain_input")"

    if [ "$NO_DOMAIN" = "true" ]; then
        DOMAIN_VALUE=""
    fi

    # interactive mode asks again for a bad domain; --yes mode fails.
    while [ -n "$DOMAIN_VALUE" ]; do
        domain_error=""
        if is_ip_address "$DOMAIN_VALUE"; then
            domain_error="You cannot get an SSL certificate for an IP; '${DOMAIN_VALUE}' is an IP."
        elif [ "$DOMAIN_VALUE" = "localhost" ] || [ "${DOMAIN_VALUE#*.}" = "local" ]; then
            domain_error="'${DOMAIN_VALUE}' is not a public domain and Let's Encrypt will not issue a certificate for it."
        elif ! validate_domain "$DOMAIN_VALUE"; then
            domain_error="The domain '${DOMAIN_VALUE}' is not valid; the right format is: bot.example.com"
        fi

        [ -n "$domain_error" ] || break

        err "$domain_error"
        if [ "$ASSUME_YES" = "true" ] || [ "$DOMAIN_FROM_PROMPT" = "" ]; then
            say "  In this mode leave the domain empty so the bot runs without SSL"
            say "  and the panel is reachable on http://${PUBLIC_IP}:${PANEL_PORT_DEFAULT}."
            if [ "$ASSUME_YES" = "true" ]; then exit 2; fi
            DOMAIN_VALUE=""
            break
        fi
        say "  To run without a domain, just press Enter."
        domain_input="$(ask "Domain or subdomain (empty = no domain, no SSL)" "")"
        DOMAIN_VALUE="$(normalize_domain "$domain_input")"
    done

    # -- Let's Encrypt email (only when a domain is set) --------------------------
    if [ -n "$DOMAIN_VALUE" ]; then
        if [ -n "$OPT_ACME_EMAIL" ]; then
            acme_input="$OPT_ACME_EMAIL"
        elif [ "$ASSUME_YES" = "true" ]; then
            acme_input="${OLD_ACME_EMAIL:-}"
        else
            acme_input="$(ask "Email for Let's Encrypt (optional)" "${OLD_ACME_EMAIL:-}" \
                "used to warn you before the certificate expires; leaving it empty is fine")"
        fi
        acme_input="$(printf '%s' "$acme_input" | tr -d '[:space:]')"
        if [ -z "$acme_input" ]; then
            warn "No Let's Encrypt email was set; the certificate is still issued but no expiry warning will arrive."
            dim "  (to add one later, just set ACME_EMAIL in .env and bring the services up again)"
        elif ! is_email "$acme_input"; then
            warn "The email '${acme_input}' does not look valid and was ignored."
            acme_input=""
        fi
        ACME_EMAIL_VALUE="$acme_input"
    else
        ACME_EMAIL_VALUE=""
    fi

    # -- final domain/TLS values --------------------------------------------
    if [ -n "$DOMAIN_VALUE" ]; then
        TLS_ENABLED="true"
        panel_url="https://${DOMAIN_VALUE}"
        panel_bind="$TLS_BIND"
        ok "Domain saved: ${DOMAIN_VALUE} — automatic SSL via Caddy is enabled."
    else
        TLS_ENABLED="false"
        panel_bind="$PANEL_BIND_DEFAULT"
        if [ -n "${OPT_DOMAIN:-}" ] || [ "${NO_DOMAIN:-false}" = "true" ]; then
            dim "  No domain: the bot runs in polling mode and the panel on http://${PUBLIC_IP}:${panel_port}."
        fi
    fi
    PANEL_BIND_VALUE="$panel_bind"
    DOMAIN_VALUE_FINAL="$DOMAIN_VALUE"
    ACME_EMAIL_VALUE_FINAL="$ACME_EMAIL_VALUE"

    # -- write the values into .env ----------------------------------------------
    # nothing has been written yet: if the validation above fails, no
    # half-finished install or partial .env is left behind on the server.
    if [ -f .env ]; then
        # --force or an interactive rebuild: leave the current .env where it is
        cp .env.example .env.new
        target=".env.new"
    else
        target=".env"
        cp .env.example "$target"
    fi

    env_set "$target" SECRET_KEY "$secret_key"
    env_set "$target" POSTGRES_PASSWORD "$pg_password"
    env_set "$target" WEBHOOK_SECRET "$webhook_secret"
    env_set "$target" BOT_TOKEN "$bot_token"
    env_set "$target" ADMIN_IDS "$admin_ids"
    env_set "$target" SUPPORT_IDS "$support_ids"
    env_set "$target" PANEL_PORT "$panel_port"
    env_set "$target" PANEL_BASE_URL "$panel_url"
    env_set "$target" OWNER_USERNAME "$owner_username"
    env_set "$target" OWNER_PASSWORD "$owner_password"
    env_set "$target" APP_NAME "$app_name"
    env_set "$target" ENV "production"

    # -- domain and SSL --------------------------------------------------------
    env_set "$target" DOMAIN "$DOMAIN_VALUE"
    env_set "$target" ACME_EMAIL "$ACME_EMAIL_VALUE"
    env_set "$target" PANEL_BIND "$PANEL_BIND_VALUE"
    if [ -n "$DOMAIN_VALUE" ]; then
        # webhook mode: Telegram must be able to reach us over HTTPS
        env_set "$target" BOT_MODE "webhook"
        env_set "$target" WEBHOOK_BASE_URL "https://${DOMAIN_VALUE}"
        env_set "$target" PANEL_BEHIND_PROXY "true"
    else
        env_set "$target" BOT_MODE "polling"
        env_set "$target" WEBHOOK_BASE_URL ""
        env_set "$target" PANEL_BEHIND_PROXY "false"
    fi

    chmod 600 "$target" 2>/dev/null || true
    if [ "$target" != ".env" ]; then
        mv -f "$target" .env
        chmod 600 .env 2>/dev/null || true
    fi

    # clear the sensitive variables from this session's environment
    OWNER_PASSWORD_SHOWN="$owner_password"
    OWNER_USERNAME_SHOWN="$owner_username"

    secret_len="${#secret_key}"
    ok ".env written (SECRET_KEY is ${secret_len} characters)."
    return 0
}

# ===========================================================================
#  Bring the stack up
# ===========================================================================
compose_up() {
    step "Pulling the base images (Postgres, Redis and Caddy)"
    # on a first run our own image may not be in the registry yet; ignore that error.
    $COMPOSE_PROFILE pull --ignore-pull-failures 2>/dev/null || $COMPOSE_PROFILE pull || true
    ok "Base images are ready."

    if [ "$TLS_ENABLED" = "true" ]; then
        step "Building the bot image and starting the services + Caddy (automatic SSL)"
    else
        step "Building the bot image and starting the services"
    fi
    dim "  (the first run takes a few minutes; the Python dependencies are compiled)"
    if ! $COMPOSE_PROFILE up -d --build; then
        err "Starting the services failed."
        return 1
    fi
    ok "Services started."
    return 0
}

wait_for_health() {
    # wait_for_health <url> [timeout]
    local url="$1" timeout="${2:-$HEALTH_TIMEOUT_DEFAULT}" health_port
    local waited=0 interval=3

    health_port="$(printf '%s' "$url" | sed -e 's|^[a-z]*://||' -e 's|/.*$||' -e 's|:.*$||')"
    step "Waiting for the panel to become ready (up to ${timeout} seconds)"
    dim "  Health check URL: $url"
    if [ "$TLS_ENABLED" = "true" ]; then
        dim "  (the first request may take a few seconds while Caddy gets the SSL certificate from Let's Encrypt)"
    fi

    while [ "$waited" -lt "$timeout" ]; do
        if command -v curl >/dev/null 2>&1; then
            if curl -fsS --max-time 10 "$url" >/dev/null 2>&1; then
                return 0
            fi
        elif command -v wget >/dev/null 2>&1; then
            if wget -q -T 5 -O /dev/null "$url" 2>/dev/null; then
                return 0
            fi
        else
            # no curl/wget: only check the service status
            if $COMPOSE ps --status running 2>/dev/null | grep -q "bot"; then
                sleep 5
                return 0
            fi
        fi
        sleep "$interval"
        waited=$(( waited + interval ))
        if [ $(( waited % 30 )) -eq 0 ]; then
            dim "  … ${waited} seconds"
        fi
    done
    return 1
}

show_tls_failure_help() {
    say ""
    err "The panel did not answer over HTTPS, so the SSL certificate was not issued."
    say ""
    printf '%s── last 40 lines of the Caddy log ─────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE --profile tls logs --tail=40 caddy 2>&1 || true
    say ""
    printf '%s── last 20 lines of the bot log ───────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE --profile tls logs --tail=20 bot 2>&1 || true
    say ""
    info "SSL troubleshooting checklist (check in this order):"
    say "  1) DNS record: there must be an A record from '${DOMAIN_FINAL}' to ${PUBLIC_IP}."
    say "     Check:  dig +short ${DOMAIN_FINAL}"
    say "  2) Port 80 must be open from the internet; Let's Encrypt connects to"
    say "     http://${DOMAIN_FINAL}/.well-known/acme-challenge/... to verify domain ownership."
    say "     Check:  ufw allow 80/tcp  and  ufw allow 443/tcp"
    say "  3) Let's Encrypt rate limit: if you have retried several times in a row,"
    say "     wait about an hour (weekly limit: 5 certificates per domain)."
    say "  4) If you use a proxy/CDN such as Cloudflare, set it to DNS only for now."
    say ""
    dim "After fixing the problem, run again:  bash install.sh   or   bash scripts/set-domain.sh ${DOMAIN_FINAL}"
    say ""
}

show_failure_diagnostics() {
    say ""
    err "The stack did not come up; the details below help with troubleshooting:"
    say ""
    printf '%s── service status ─────────────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE ps 2>&1 | tail -n 20 || true
    say ""
    printf '%s── last 40 lines of the bot log ───────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE logs --tail=40 bot 2>&1 || true
    say ""
    printf '%s── last 20 lines of the database log ──────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE logs --tail=20 db 2>&1 || true
    say ""
    info "Things that usually fix the problem:"
    say "  • Is the bot token correct? (a 401 in the log means the token is wrong)"
    say "  • Is port ${PANEL_PORT_DEFAULT} free?  ss -lntp | grep ${PANEL_PORT_DEFAULT}"
    say "  • Is there enough disk space?  df -h /"
    say "  • Try again:  $COMPOSE up -d --build"
    say ""
    dim "To follow the live log:  $COMPOSE logs -f bot"
}

success_box() {
    local port="$1" url="$2" username="$3" password="$4" ip="$5"
    local line="════════════════════════════════════════════════════════════════"

    say ""
    printf '%s%s%s\n' "$C_GREEN" "$line" "$C_RESET"
    printf '%s%s  ✅  Installation finished successfully%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
    printf '%s%s%s\n' "$C_GREEN" "$line" "$C_RESET"
    say ""
    printf '  %s🌐 Admin panel URL:%s\n' "$C_BOLD" "$C_RESET"
    printf '     %s%s/panel/login%s\n' "$C_CYAN" "$url" "$C_RESET"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '  %s🔒 SSL is active and %srenews automatically%s (about 30 days before expiry).%s\n' \
            "$C_BOLD" "$C_GREEN" "$C_RESET" "$C_RESET"
        if [ "$CERT_ISSUED" = "true" ] && [ -n "$CERT_DETAILS" ]; then
            printf '     %s%s%s\n' "$C_DIM" "$CERT_DETAILS" "$C_RESET"
        fi
        printf '     %sTo see the certificate status: bash scripts/set-domain.sh --status%s\n' "$C_DIM" "$C_RESET"
    elif [ -n "$ip" ] && [ "$ip" != "SERVER_IP" ] && [ "$url" != "http://${ip}:${port}" ]; then
        printf '     %s(temporary URL without a domain: http://%s:%s/panel/login)%s\n' "$C_DIM" "$ip" "$port" "$C_RESET"
    fi
    say ""
    printf '  %s👤 Owner username:%s %s%s%s\n' "$C_BOLD" "$C_RESET" "$C_CYAN" "$username" "$C_RESET"
    printf '  %s🔑 Owner password:%s   %s%s%s\n' "$C_BOLD" "$C_RESET" "$C_CYAN" "$password" "$C_RESET"
    if [ "$REUSE_ENV" = "true" ]; then
        printf '     %sThe value above was read from .env and only applies to the first install.%s\n' "$C_DIM" "$C_RESET"
        printf '     %sIf you changed the password from the panel, this one is no longer valid.%s\n' "$C_DIM" "$C_RESET"
        printf '     %sPassword reset: the "Reset owner password" section in docs/DEPLOYMENT.md%s\n' "$C_DIM" "$C_RESET"
    fi
    say ""
    printf '  %s%s⚠  Save this password somewhere safe right now, and after the first sign-in%s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
    printf '  %s%s   change it from inside the panel (Account → Change password).%s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
    say ""
    printf '%s%s%s\n' "$C_GREEN" "$line" "$C_RESET"
    say ""
    printf '  %s🧰 Common commands (inside %s):%s\n' "$C_BOLD" "$INSTALL_DIR" "$C_RESET"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '     %s%s--profile tls logs -f bot%s      %s→ follow the bot log%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s--profile tls logs -f caddy%s    %s→ follow the SSL certificate log%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s restart bot%s      %s→ restart the bot%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s down%s             %s→ stop the services (data is kept)%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
    else
        printf '     %s%s logs -f bot%s      %s→ follow the bot log%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s restart bot%s      %s→ restart the bot%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s down%s             %s→ stop the services (data is kept)%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
    fi
    printf '     %s%s ps%s               %s→ service status%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
    say ""
    printf '  %s💾 Automatic backups are on (every %s hours) and the files are stored in %s.%s\n' \
        "$C_BOLD" "${BACKUP_HOURS_SHOWN:-24}" "$(printf '%s/backups' "$INSTALL_DIR")" "$C_RESET"
    printf '  %s🔄 To update later: %s%s\n' "$C_BOLD" "bash update.sh" "$C_RESET"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '  %s🌐 To change or remove the domain: bash scripts/set-domain.sh --status%s\n' "$C_BOLD" "$C_RESET"
    else
        printf '  %s🔒 To enable a domain and automatic SSL: bash scripts/set-domain.sh example.com%s\n' "$C_BOLD" "$C_RESET"
    fi
    say ""
    if [ "$ALLOW_UFW_HINT" = "true" ]; then
        printf '  %s🔥 If you have a firewall (ufw) enabled, open the ports:%s\n' "$C_BOLD" "$C_RESET"
        if [ "$TLS_ENABLED" = "true" ]; then
            printf '     %sufw allow 80/tcp && ufw allow 443/tcp%s\n' "$C_CYAN" "$C_RESET"
        else
            printf '     %sufw allow %s/tcp%s\n' "$C_CYAN" "$port" "$C_RESET"
        fi
        say ""
    fi
    printf '  %s🗑  To remove everything: bash uninstall.sh   (data is kept by default)%s\n' "$C_DIM" "$C_RESET"
    return 0
}

firewall_hint_needed() {
    ALLOW_UFW_HINT="false"
    if command -v ufw >/dev/null 2>&1; then
        if ufw status 2>/dev/null | grep -qi '^Status: active'; then
            ALLOW_UFW_HINT="true"
        fi
    fi
}

# ===========================================================================
#  Main
# ===========================================================================
main() {
    parse_args "$@"

    if [ "$SHOW_HELP" = "true" ]; then
        usage
        exit 0
    fi

    read_app_version
    banner

    require_root "$@"
    install_docker
    if ! detect_compose; then
        err "The Docker Compose plugin was not found."
        say "  Install it: apt-get update && apt-get install -y docker-compose-plugin"
        say "  Then run this script again."
        exit 1
    fi
    ok "Docker is ready (${COMPOSE})."

    prepare_dir
    load_existing

    REUSE_ENV="false"
    ENV_BACKUP=""
    handle_existing_env

    detect_public_ip
    if [ "$PUBLIC_IP" != "SERVER_IP" ]; then
        dim "  Server IP: ${PUBLIC_IP}"
    fi

    if [ "$REUSE_ENV" = "true" ]; then
        BOT_TOKEN_FINAL="$(env_get .env BOT_TOKEN)"
        ADMIN_IDS_FINAL="$(env_get .env ADMIN_IDS)"
        PANEL_PORT_FINAL="$(env_get .env PANEL_PORT)"
        OWNER_USERNAME_SHOWN="$(env_get .env OWNER_USERNAME)"
        OWNER_PASSWORD_SHOWN="$(env_get .env OWNER_PASSWORD)"
        PANEL_URL_FINAL="$(env_get .env PANEL_BASE_URL)"
        BACKUP_HOURS_SHOWN="$(env_get .env BACKUP_INTERVAL_HOURS)"
        DOMAIN_FINAL="$(normalize_domain "$(env_get .env DOMAIN)")"
        ACME_EMAIL_FINAL="$(env_get .env ACME_EMAIL)"
        PANEL_BIND_FINAL="$(env_get .env PANEL_BIND)"
    else
        write_env
        BOT_TOKEN_FINAL="$(env_get .env BOT_TOKEN)"
        ADMIN_IDS_FINAL="$(env_get .env ADMIN_IDS)"
        PANEL_PORT_FINAL="$(env_get .env PANEL_PORT)"
        PANEL_URL_FINAL="$(env_get .env PANEL_BASE_URL)"
        BACKUP_HOURS_SHOWN="$(env_get .env BACKUP_INTERVAL_HOURS)"
        DOMAIN_FINAL="$(normalize_domain "$(env_get .env DOMAIN)")"
        ACME_EMAIL_FINAL="$(env_get .env ACME_EMAIL)"
        PANEL_BIND_FINAL="$(env_get .env PANEL_BIND)"
    fi

    [ -n "$PANEL_PORT_FINAL" ] || PANEL_PORT_FINAL="$PANEL_PORT_DEFAULT"
    [ -n "$OWNER_USERNAME_SHOWN" ] || OWNER_USERNAME_SHOWN="$OWNER_USERNAME_DEFAULT"
    [ -n "$PANEL_URL_FINAL" ] || PANEL_URL_FINAL="http://127.0.0.1:${PANEL_PORT_FINAL}"
    [ -n "$BACKUP_HOURS_SHOWN" ] || BACKUP_HOURS_SHOWN="24"

    # -- domain / no-domain mode -------------------------------------------
    if [ -n "$DOMAIN_FINAL" ]; then
        TLS_ENABLED="true"
        COMPOSE_PROFILE="$COMPOSE --profile tls"
        PANEL_BIND_FINAL="$TLS_BIND"
    else
        TLS_ENABLED="false"
        COMPOSE_PROFILE="$COMPOSE"
        [ -n "$PANEL_BIND_FINAL" ] || PANEL_BIND_FINAL="$PANEL_BIND_DEFAULT"
    fi

    # configuration summary before the run
    say ""
    printf '%s── configuration summary ──────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    printf '  Install dir   : %s\n' "$INSTALL_DIR"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '  Domain        : %s%s%s (automatic SSL)\n' "$C_CYAN" "$DOMAIN_FINAL" "$C_RESET"
        printf '  ACME email    : %s\n' "${ACME_EMAIL_FINAL:-(not set — no expiry warning)}"
        printf '  Bot mode      : webhook\n'
    else
        printf '  Domain        : (no domain — no SSL)\n'
        printf '  Bot mode      : polling\n'
    fi
    printf '  Panel port    : %s\n' "$PANEL_PORT_FINAL"
    printf '  Public URL    : %s\n' "$PANEL_URL_FINAL"
    printf '  Panel owner   : %s\n' "$OWNER_USERNAME_SHOWN"
    printf '  Admins        : %s\n' "$ADMIN_IDS_FINAL"
    if [ -n "${BOT_TOKEN_FINAL:-}" ]; then
        printf '  Bot token     : %s…%s (hidden)\n' "$(printf '%s' "$BOT_TOKEN_FINAL" | cut -c1-10)" "$(printf '%s' "$BOT_TOKEN_FINAL" | rev | cut -c1-4 | rev)"
    fi
    printf '%s───────────────────────────────────────────────────%s\n' "$C_BOLD" "$C_RESET"

    # -- check DNS before requesting the certificate ------------------------------------
    if [ "$TLS_ENABLED" = "true" ]; then
        step "Checking that the domain points to this server"
        if check_dns_points_here "$DOMAIN_FINAL"; then
            ok "The domain points to this server (${PUBLIC_IP})."
        else
            show_dns_warning
            if [ "$ASSUME_YES" = "true" ]; then
                warn "--yes mode: continuing without confirmation; if DNS is not ready, no certificate will be issued."
            elif ! confirm "Continue anyway? (if DNS is not ready yet, fix it first)" "n"; then
                say ""
                info "Install stopped. After fixing the DNS record, run again:"
                dim "    bash install.sh --domain ${DOMAIN_FINAL}"
                exit 0
            fi
        fi
    fi

    if ! compose_up; then
        show_failure_diagnostics
        exit 1
    fi

    if [ "$TLS_ENABLED" = "true" ]; then
        if wait_for_health "https://${DOMAIN_FINAL}/healthz" "$HTTPS_TIMEOUT_DEFAULT"; then
            if cert_status "$DOMAIN_FINAL"; then
                CERT_ISSUED="true"
            fi
            firewall_hint_needed
            success_box "$PANEL_PORT_FINAL" "$PANEL_URL_FINAL" "$OWNER_USERNAME_SHOWN" "$OWNER_PASSWORD_SHOWN" "$PUBLIC_IP"
        else
            show_tls_failure_help
            exit 1
        fi
    else
        if wait_for_health "http://127.0.0.1:${PANEL_PORT_FINAL}/healthz" "$HEALTH_TIMEOUT_DEFAULT"; then
            firewall_hint_needed
            success_box "$PANEL_PORT_FINAL" "$PANEL_URL_FINAL" "$OWNER_USERNAME_SHOWN" "$OWNER_PASSWORD_SHOWN" "$PUBLIC_IP"
        else
            show_failure_diagnostics
            exit 1
        fi
    fi
}

main "$@"
