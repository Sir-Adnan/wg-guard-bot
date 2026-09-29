#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — change/disable the domain and inspect the TLS certificate
#
#  usage:
#      bash scripts/set-domain.sh example.com        # set or change the domain
#      bash scripts/set-domain.sh --disable          # return to no-domain mode
#      bash scripts/set-domain.sh --status           # domain and certificate status
#      bash scripts/set-domain.sh --renew            # reload the Caddy configuration
#
#  Caddy obtains the TLS certificate itself and renews it automatically (about 30 days
#  before expiry); no certbot, cron or manual work is needed.
#
#  This script only edits the .env file and brings the services back up.
#  Data volumes (pgdata, redisdata, backups, caddy_data) are left untouched.
# ---------------------------------------------------------------------------
set -euo pipefail

# Project root: one directory above scripts/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE=""
DOMAIN_ARG=""
ASSUME_YES="false"
HTTPS_TIMEOUT="180"

# ===========================================================================
#  colours — only when the output is a terminal and NO_COLOR is not set
# ===========================================================================
if [ -n "${NO_COLOR:-}" ] || [ ! -t 1 ]; then
    C_RESET=""; C_BOLD=""; C_DIM=""
    C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_CYAN=""
else
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
${C_BOLD}WG-Guard Bot — domain and TLS certificate${C_RESET}

${C_BOLD}Usage:${C_RESET}
  bash scripts/set-domain.sh <domain>    set or change the domain (automatic SSL)
  bash scripts/set-domain.sh --disable   remove the domain and return to polling mode
  bash scripts/set-domain.sh --status    show the domain and certificate status
  bash scripts/set-domain.sh --renew     reload the Caddy configuration

${C_BOLD}Options:${C_RESET}
  ${C_CYAN}--yes${C_RESET}      skip the confirmation prompt (for automated scripts)
  ${C_CYAN}-h, --help${C_RESET} show this help

${C_BOLD}Examples:${C_RESET}
  bash scripts/set-domain.sh bot.example.com
  bash scripts/set-domain.sh https://bot.example.com/     # the scheme and trailing slash are stripped automatically

${C_BOLD}Notes:${C_RESET}
  • Before running, create an A record pointing the domain to the server IP.
  • Ports 80 and 443 must be open to the internet.
  • The TLS certificate is renewed automatically; no certbot or cron is needed.

${C_DIM}Sets, changes or disables the domain for WG-Guard Bot and inspects the
Caddy-managed certificate.  Editing .env is atomic and keeps a timestamped
backup.${C_RESET}
EOF
}

# ===========================================================================
#  helpers
# ===========================================================================
require_root() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        warn "This script needs root access; re-running it with sudo…"
        exec sudo -E bash "$0" "$@"
    fi
    err "Root access is required to change the services (and sudo is not installed)."
    exit 1
}

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    else
        err "Docker Compose not found. Install: apt-get install -y docker-compose-plugin"
        exit 1
    fi
}

running_services() {
    # Names of the running services (without the tls profile, so Caddy is visible too)
    $COMPOSE --profile tls ps --services --status running 2>/dev/null || true
}

normalize_domain() {
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's|^[[:space:]]*||' -e 's|[[:space:]]*$||' \
              -e 's|^[a-z][a-z0-9+.-]*://||' \
              -e 's|/.*$||' \
              -e 's|^\.*||' -e 's|\.*$||'
}

validate_domain() {
    printf '%s' "$1" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
}

is_ip_address() {
    printf '%s' "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
}

is_private_ip() {
    # Private/reserved addresses — a sign of DNS hijacking or a local resolver, not a
    # real A record, and they make the comparison with the public IP of the server meaningless.
    local ip="$1" a b
    case "$ip" in
        10.*|127.*|169.254.*|192.168.*|0.*) return 0 ;;
    esac
    a="$(printf '%s' "$ip" | cut -d. -f1)"
    b="$(printf '%s' "$ip" | cut -d. -f2)"
    if [ "$a" = "172" ] && [ "$b" -ge 16 ] 2>/dev/null && [ "$b" -le 31 ] 2>/dev/null; then
        return 0
    fi
    case "$ip" in
        192.0.2.*|198.51.100.*|203.0.113.*) return 0 ;;
    esac
    return 1
}

detect_public_ip() {
    PUBLIC_IP=""
    if command -v curl >/dev/null 2>&1; then
        PUBLIC_IP="$(curl -fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
        if [ -z "$PUBLIC_IP" ]; then
            PUBLIC_IP="$(curl -fsS --max-time 6 https://ifconfig.me/ip 2>/dev/null || true)"
        fi
    fi
    if [ -z "$PUBLIC_IP" ] && command -v hostname >/dev/null 2>&1; then
        PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    fi
    [ -n "$PUBLIC_IP" ] || PUBLIC_IP="SERVER_IP"
    return 0
}

resolve_domain_ips() {
    # Public IPs the domain resolves to (private/reserved addresses are filtered out)
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

# -- reading and writing .env ------------------------------------------------------
env_get() {
    local file="$1" key="$2" line=""
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

env_set() {
    # env_set <file> <key> <value> — replace the whole line or append it
    local file="$1" key="$2" value="$3" escaped=""
    if grep -Eq "^[[:space:]]*${key}=" "$file"; then
        escaped="$(printf '%s' "$value" | sed -e 's/[&\\]/\\&/g')"
        sed -i "s|^[[:space:]]*${key}=.*|${key}=${escaped}|" "$file"
    else
        printf '%s=%s\n' "$key" "$value" >> "$file"
    fi
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

apply_env_updates() {
    # apply_env_updates <domain> <email>
    # Atomic edit: write to a temporary file, then mv; with a timestamped backup.
    local new_domain="$1" email="$2" stamp backup tmp
    stamp="$(date +%Y%m%d-%H%M%S)"
    backup=".env.bak.${stamp}"

    cp -p .env "$backup" || { err "Failed to back up .env."; return 1; }
    ok ".env backup saved: ${backup}"

    tmp="$(mktemp .env.tmp.XXXXXX)" || { err "Failed to create a temporary file."; return 1; }
    cat .env > "$tmp"

    env_set "$tmp" DOMAIN "$new_domain"
    env_set "$tmp" ACME_EMAIL "$email"
    if [ -n "$new_domain" ]; then
        env_set "$tmp" PANEL_BASE_URL "https://${new_domain}"
        env_set "$tmp" WEBHOOK_BASE_URL "https://${new_domain}"
        env_set "$tmp" BOT_MODE "webhook"
        env_set "$tmp" PANEL_BEHIND_PROXY "true"
        env_set "$tmp" PANEL_BIND "127.0.0.1"
    else
        local port
        port="$(env_get .env PANEL_PORT)"
        [ -n "$port" ] || port="8080"
        detect_public_ip
        env_set "$tmp" PANEL_BASE_URL "http://${PUBLIC_IP}:${port}"
        env_set "$tmp" WEBHOOK_BASE_URL ""
        env_set "$tmp" BOT_MODE "polling"
        env_set "$tmp" PANEL_BEHIND_PROXY "false"
        env_set "$tmp" PANEL_BIND "0.0.0.0"
    fi

    chmod 600 "$tmp" 2>/dev/null || true
    mv -f "$tmp" .env
    chmod 600 .env 2>/dev/null || true
    ok ".env updated (all other lines and comments left untouched)."
    return 0
}

wait_for_url() {
    local url="$1" timeout="${2:-$HTTPS_TIMEOUT}" waited=0
    step "Waiting for ${url} to respond (up to ${timeout} seconds)"
    if [ "${url#https://}" != "$url" ]; then
        dim "  The first request may take a few seconds while the TLS certificate is issued."
    fi
    while [ "$waited" -lt "$timeout" ]; do
        if command -v curl >/dev/null 2>&1; then
            if curl -fsS --max-time 10 "$url" >/dev/null 2>&1; then
                return 0
            fi
        else
            break
        fi
        sleep 3
        waited=$(( waited + 3 ))
        if [ $(( waited % 30 )) -eq 0 ]; then
            dim "  … ${waited} seconds"
        fi
    done
    return 1
}

cert_file_path() {
    # Path of the certificate file inside the caddy_data volume
    $COMPOSE --profile tls exec -T caddy sh -c \
        "find /data/caddy/certificates -name '${1}.crt' 2>/dev/null | head -n1" 2>/dev/null | tr -d '\r' || true
}

cert_details() {
    # Certificate summary: subject/issuer/expiry + days remaining
    local domain="$1" out=""
    out="$($COMPOSE --profile tls exec -T caddy sh -c "
        f=\$(find /data/caddy/certificates -name '${domain}.crt' 2>/dev/null | head -n1)
        [ -n \"\$f\" ] || exit 1
        openssl x509 -in \"\$f\" -noout -subject -issuer -enddate
    " 2>/dev/null | tr -d '\r' || true)"
    [ -n "$out" ] || return 1
    CERT_SUBJECT="$(printf '%s\n' "$out" | sed -n 's/^subject=//p' | head -n1)"
    CERT_ISSUER="$(printf '%s\n' "$out" | sed -n 's/^issuer=//p' | head -n1)"
    CERT_END="$(printf '%s\n' "$out" | sed -n 's/^notAfter=//p' | head -n1)"
    CERT_DAYS=""
    if [ -n "$CERT_END" ]; then
        CERT_DAYS="$($COMPOSE --profile tls exec -T caddy sh -c \
            "f=\$(find /data/caddy/certificates -name '${domain}.crt' 2>/dev/null | head -n1); \
             [ -n \"\$f\" ] && openssl x509 -in \"\$f\" -noout -checkend 0 >/dev/null 2>&1 && \
             echo \$(( ( \$(date -d \"\$(openssl x509 -in \"\$f\" -noout -enddate | cut -d= -f2)\" +%s) - \$(date +%s) ) / 86400 ))" \
            2>/dev/null | tr -d '\r' || true)"
    fi
    return 0
}

# ===========================================================================
#  --status
# ===========================================================================
show_status() {
    local domain email bind mode caddy_state https_ok="false"

    domain="$(normalize_domain "$(env_get .env DOMAIN)")"
    email="$(env_get .env ACME_EMAIL)"
    bind="$(env_get .env PANEL_BIND)"
    mode="$(env_get .env BOT_MODE)"
    [ -n "$bind" ] || bind="0.0.0.0"
    [ -n "$mode" ] || mode="polling"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — domain and certificate status${C_RESET}"
    say ""

    if [ -z "$domain" ]; then
        info "Domain: not set (no-SSL mode)"
        say "   Bot mode       : ${mode}"
        say "   Panel URL      : $(env_get .env PANEL_BASE_URL)"
        say "   Caddy          : not running (the tls profile is disabled)"
        say ""
        dim "To enable a domain and automatic SSL:  bash scripts/set-domain.sh example.com"
        return 0
    fi

    info "Domain: ${domain}"
    say "   ACME email     : ${email:-(not registered — no expiry warnings)}"
    say "   Bot mode       : ${mode}"
    say "   Panel URL      : $(env_get .env PANEL_BASE_URL)"
    say "   Panel bind on host: ${bind}:$(env_get .env PANEL_PORT)"
    say ""

    # -- Caddy container status --------------------------------------------------
    if running_services | grep -q '^caddy$'; then
        caddy_state="$($COMPOSE --profile tls ps caddy --format '{{.Status}}' 2>/dev/null | head -n1 || true)"
        ok "Caddy is running ${caddy_state:+(${caddy_state})}"
    else
        warn "Caddy is not running."
        dim "  Start:  $COMPOSE --profile tls up -d"
    fi

    # -- certificate -----------------------------------------------------------------
    if cert_details "$domain"; then
        say ""
        say "   Issuer         : ${CERT_ISSUER:-unknown}"
        say "   Subject        : ${CERT_SUBJECT:-unknown}"
        say "   Expires        : ${CERT_END:-unknown}"
        if [ -n "$CERT_DAYS" ]; then
            say "   Days remaining : ${CERT_DAYS}"
        fi
        ok "A TLS certificate exists and is ${C_GREEN}renewed automatically${C_RESET} (about 30 days before expiry)."
        info "No certbot, cron or manual renewal is needed."
    else
        warn "No TLS certificate found in the caddy_data volume yet."
        dim "  If you just set the domain, the first HTTPS request creates it:"
        dim "      curl -I https://${domain}/healthz"
        dim "  and if that fails:  $COMPOSE --profile tls logs --tail=40 caddy"
    fi

    # -- public health check -----------------------------------------------------
    if command -v curl >/dev/null 2>&1; then
        say ""
        if curl -fsS --max-time 10 "https://${domain}/healthz" >/dev/null 2>&1; then
            https_ok="true"
        fi
        if [ "$https_ok" = "true" ]; then
            ok "Health check on https://${domain}/healthz succeeded."
        else
            warn "No healthy response from https://${domain}/healthz (check DNS, ports 80/443 or certificate issuance)."
        fi
    fi

    say ""
    info "To change the domain:  bash scripts/set-domain.sh new.example.com"
    info "To remove the domain:  bash scripts/set-domain.sh --disable"
    return 0
}

# ===========================================================================
#  --disable
# ===========================================================================
disable_domain() {
    local current
    current="$(normalize_domain "$(env_get .env DOMAIN)")"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — remove the domain${C_RESET}"
    say ""
    if [ -z "$current" ]; then
        info "No domain was set; there is nothing to remove."
        return 0
    fi
    say "  Current domain: ${C_CYAN}${current}${C_RESET}"
    say "  Removing the domain returns the bot to polling mode and the panel to http://IP:PORT."
    say "  ${C_BOLD}Data and certificate volumes are left untouched.${C_RESET}"
    say ""
    if ! confirm "Remove the domain and return the bot to no-domain mode?" "y"; then
        info "Cancelled; no changes were made."
        return 0
    fi

    apply_env_updates "" ""
    step "Restarting the services (without Caddy)"
    $COMPOSE up -d --remove-orphans
    # The Caddy container is no longer in the tls profile; we remove it explicitly too.
    $COMPOSE --profile tls rm -sf caddy >/dev/null 2>&1 || true
    ok "Services are running in no-domain mode."

    local port
    port="$(env_get .env PANEL_PORT)"
    [ -n "$port" ] || port="8080"
    say ""
    info "The panel is available at: http://${PUBLIC_IP}:${port}/panel/login"
    dim "  The bot needs a few seconds to come up; if it does not open:"
    dim "      $COMPOSE logs --tail=40 bot"
    dim "The previous certificate stays in the caddy_data volume but is no longer used."
    return 0
}

# ===========================================================================
#  --renew
# ===========================================================================
renew_now() {
    local domain
    domain="$(normalize_domain "$(env_get .env DOMAIN)")"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — renew the certificate${C_RESET}"
    say ""
    if [ -z "$domain" ]; then
        err "No domain is set; set the domain first:"
        dim "    bash scripts/set-domain.sh example.com"
        return 1
    fi

    info "Caddy renews the TLS certificate ${C_GREEN}automatically${C_RESET} — about 30 days before expiry."
    dim "  This command only reloads the Caddy configuration and shows the expiry date;"
    dim "  certificates are not deleted (that puts pressure on Let's Encrypt and you may get rate-limited)."
    say ""

    step "Reloading the Caddy configuration"
    if $COMPOSE --profile tls exec -T caddy caddy reload --config /etc/caddy/Caddyfile; then
        ok "Caddy configuration reloaded (no downtime)."
    else
        warn "Reload failed; is Caddy running?"
        dim "  Status:  $COMPOSE --profile tls ps caddy"
        dim "  Logs:    $COMPOSE --profile tls logs --tail=40 caddy"
        return 1
    fi

    say ""
    if cert_details "$domain"; then
        info "Certificate status for '${domain}':"
        say "   Issuer : ${CERT_ISSUER:-unknown}"
        say "   Expires: ${CERT_END:-unknown}"
        [ -n "$CERT_DAYS" ] && say "   Days remaining: ${CERT_DAYS}"
        ok "Automatic renewal is active; nothing else is needed."
    else
        warn "Certificate not found. To issue the first one, send an HTTPS request:"
        dim "    curl -I https://${domain}/healthz"
    fi
    return 0
}

# ===========================================================================
#  set/change the domain
# ===========================================================================
set_domain() {
    local new_domain="$1" email="" current="" dns_ok="false" resolved=""

    new_domain="$(normalize_domain "$new_domain")"

    if [ -z "$new_domain" ]; then
        err "The domain is empty. Use --disable to remove the domain."
        return 2
    fi
    if is_ip_address "$new_domain"; then
        err "'${new_domain}' is an IP; Let's Encrypt does not issue certificates for IPs."
        say "  To run without a domain:  bash scripts/set-domain.sh --disable"
        return 2
    fi
    if [ "$new_domain" = "localhost" ] || [ "${new_domain#*.}" = "local" ]; then
        err "'${new_domain}' is not a public domain; Let's Encrypt will not issue a certificate for it."
        return 2
    fi
    if ! validate_domain "$new_domain"; then
        err "The domain '${new_domain}' is not valid. Correct format: bot.example.com"
        return 2
    fi

    printf '%s\n' "${C_BOLD}WG-Guard Bot — set the domain${C_RESET}"
    say ""

    detect_public_ip
    current="$(normalize_domain "$(env_get .env DOMAIN)")"
    if [ -n "$current" ] && [ "$current" != "$new_domain" ]; then
        warn "The domain changes from '${current}' to '${new_domain}'."
        say "   • The Telegram webhook is registered on the new domain automatically the next time the bot starts."
        say "   • The certificate for the previous domain stays in the caddy_data volume but is no longer used."
        say ""
        if ! confirm "Continue?" "y"; then
            info "Cancelled; no changes were made."
            return 0
        fi
    fi

    # -- ACME email ------------------------------------------------------------
    email="$(env_get .env ACME_EMAIL)"
    if [ "$ASSUME_YES" != "true" ]; then
        printf '%s? %s%s %s[%s]%s\n  %s❯%s ' \
            "$C_BOLD" "Email for Let's Encrypt (optional)" "$C_RESET" \
            "$C_DIM" "${email:-(empty)}" "$C_RESET" "$C_GREEN" "$C_RESET" >&2
        local answer=""
        IFS= read -r answer || answer=""
        if [ -n "$answer" ]; then
            email="$answer"
        fi
    fi
    email="$(printf '%s' "$email" | tr -d '[:space:]')"
    if [ -z "$email" ]; then
        warn "No email was registered; the certificate is still issued but no expiry warnings arrive."
    fi

    # -- DNS check -------------------------------------------------------------
    step "Checking DNS for ${new_domain}"
    say "   IP this server: ${PUBLIC_IP}"
    resolved="$(resolve_domain_ips "$new_domain" | tr '\n' ' ' | sed -e 's/[[:space:]]*$//')"
    say "   IP domain     : ${resolved:-not found}"
    if [ -n "$resolved" ] && [ "$PUBLIC_IP" != "SERVER_IP" ]; then
        local one
        for one in $resolved; do
            if [ "$one" = "$PUBLIC_IP" ]; then
                dns_ok="true"
                break
            fi
        done
    fi

    if [ "$dns_ok" = "true" ]; then
        ok "The domain points to this server correctly."
    else
        warn "The domain does not point to this server yet."
        dim "  Create an A record:  ${new_domain}  A  ${PUBLIC_IP}"
        if [ "$ASSUME_YES" != "true" ]; then
            if ! confirm "Continue with this state? (no certificate is issued until DNS is fixed)" "n"; then
                info "Cancelled. Run it again after fixing DNS:"
                dim "    bash scripts/set-domain.sh ${new_domain}"
                return 0
            fi
        else
            warn "--yes mode: continuing without confirmation."
        fi
    fi

    # -- writing .env and starting the services --------------------------------------
    step "Updating the .env file"
    apply_env_updates "$new_domain" "$email"

    step "Starting the services with Caddy (automatic SSL)"
    $COMPOSE --profile tls up -d --remove-orphans
    ok "Services started."

    say ""
    if wait_for_url "https://${new_domain}/healthz"; then
        ok "The panel is available over HTTPS: https://${new_domain}/panel/login"
        if cert_details "$new_domain"; then
            say ""
            say "   Issuer : ${CERT_ISSUER:-unknown}"
            say "   Expires: ${CERT_END:-unknown}"
            [ -n "$CERT_DAYS" ] && say "   Days remaining: ${CERT_DAYS}"
        fi
        say ""
        ok "The TLS certificate is ${C_GREEN}renewed automatically${C_RESET}; no certbot or cron is needed."
    else
        say ""
        err "The panel did not respond over HTTPS; certificate issuance did not complete."
        say ""
        printf '%s── last 40 lines of the Caddy log ───────────────────────────%s\n' "$C_BOLD" "$C_RESET"
        $COMPOSE --profile tls logs --tail=40 caddy 2>&1 || true
        say ""
        info "Checklist:"
        say "  1) Does the domain's A record point to ${PUBLIC_IP}?  dig +short ${new_domain}"
        say "  2) Are ports 80 and 443 open?  ufw allow 80/tcp && ufw allow 443/tcp"
        say "  3) Let's Encrypt rate limit? (several attempts in a row = wait about an hour)"
        say "  4) A CDN/proxy such as Cloudflare must be in DNS only mode."
        return 1
    fi
    return 0
}

# ===========================================================================
#  main
# ===========================================================================
main() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --yes|-y)   ASSUME_YES="true" ;;
            --disable)  MODE="disable" ;;
            --status)   MODE="status" ;;
            --renew)    MODE="renew" ;;
            --help|-h)  usage; exit 0 ;;
            --*)        err "Unknown option: $1"; say ""; usage; exit 2 ;;
            *)          DOMAIN_ARG="$1"; MODE="set" ;;
        esac
        shift
    done

    if [ -z "$MODE" ]; then
        usage
        exit 2
    fi

    cd "$PROJECT_DIR" || { err "Project directory not found: $PROJECT_DIR"; exit 1; }
    if [ ! -f .env ]; then
        err ".env file not found in '${PROJECT_DIR}'; install first: bash install.sh"
        exit 1
    fi
    if [ ! -f docker-compose.yml ]; then
        err "docker-compose.yml not found; run this script from inside the project directory."
        exit 1
    fi

    require_root "$@"
    detect_compose

    case "$MODE" in
        status)  show_status ;;
        disable) disable_domain ;;
        renew)   renew_now ;;
        set)     set_domain "$DOMAIN_ARG" ;;
    esac
}

main "$@"
