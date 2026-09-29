#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — تغییر یا حذف دامنه و مدیریت گواهی SSL
#  WG-Guard Bot — change/disable the domain and inspect the TLS certificate.
#
#  استفاده / usage:
#      bash scripts/set-domain.sh example.com        # تعیین یا تغییر دامنه
#      bash scripts/set-domain.sh --disable          # بازگشت به حالت بدون دامنه
#      bash scripts/set-domain.sh --status           # وضعیت دامنه و گواهی
#      bash scripts/set-domain.sh --renew            # بارگذاری دوباره‌ی تنظیمات Caddy
#
#  گواهی SSL را Caddy خودش می‌گیرد و خودش تمدید می‌کند (حدود ۳۰ روز قبل از
#  انقضا)؛ نیازی به certbot، cron یا کار دستی نیست.
#
#  این اسکریپت فقط فایل .env را ویرایش می‌کند و سرویس‌ها را دوباره بالا می‌آورد.
#  حجم‌های داده (pgdata، redisdata، backups، caddy_data) دست‌نخورده می‌مانند.
# ---------------------------------------------------------------------------
set -euo pipefail

# ریشه‌ی پروژه: یک پوشه بالاتر از scripts/
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

MODE=""
DOMAIN_ARG=""
ASSUME_YES="false"
HTTPS_TIMEOUT="180"

# ===========================================================================
#  رنگ‌ها / colours — فقط وقتی خروجی یک ترمینال باشد و NO_COLOR تنظیم نشده باشد
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
${C_BOLD}WG-Guard Bot — دامنه و گواهی SSL${C_RESET}

${C_BOLD}استفاده:${C_RESET}
  bash scripts/set-domain.sh <دامنه>     تعیین یا تغییر دامنه (SSL خودکار)
  bash scripts/set-domain.sh --disable   حذف دامنه و بازگشت به حالت polling
  bash scripts/set-domain.sh --status    نمایش وضعیت دامنه و گواهی
  bash scripts/set-domain.sh --renew     بارگذاری دوباره‌ی تنظیمات Caddy

${C_BOLD}گزینه‌ها:${C_RESET}
  ${C_CYAN}--yes${C_RESET}      بدون پرسش تأیید (برای اسکریپت‌های خودکار)
  ${C_CYAN}-h, --help${C_RESET} نمایش همین راهنما

${C_BOLD}نمونه‌ها:${C_RESET}
  bash scripts/set-domain.sh bot.example.com
  bash scripts/set-domain.sh https://bot.example.com/     # طرح و اسلش خودکار حذف می‌شود

${C_BOLD}نکته‌ها:${C_RESET}
  • قبل از اجرا، یک رکورد A از دامنه به IP سرور بسازید.
  • پورت‌های ۸۰ و ۴۴۳ باید از اینترنت باز باشند.
  • گواهی SSL خودکار تمدید می‌شود؛ نیازی به certbot یا cron نیست.

${C_DIM}English: set/change/disable the domain for WG-Guard Bot and inspect the
Caddy-managed certificate.  Editing .env is atomic and keeps a timestamped
backup.${C_RESET}
EOF
}

# ===========================================================================
#  کمکی‌ها / helpers
# ===========================================================================
require_root() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        warn "این اسکریپت به دسترسی root نیاز دارد؛ با sudo دوباره اجرا می‌شود…"
        exec sudo -E bash "$0" "$@"
    fi
    err "برای تغییر سرویس‌ها به دسترسی root نیاز است (و sudo نصب نیست)."
    exit 1
}

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
    elif command -v docker-compose >/dev/null 2>&1; then
        COMPOSE="docker-compose"
    else
        err "Docker Compose پیدا نشد. نصب: apt-get install -y docker-compose-plugin"
        exit 1
    fi
}

running_services() {
    # نام سرویس‌های در حال اجرا (بدون پروفایل tls تا Caddy هم دیده شود)
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
    # آدرس‌های خصوصی/رزرو‌شده — نشانه‌ی DNS hijack یا رزولور محلی هستند، نه یک
    # رکورد A واقعی، و مقایسه با IP عمومی سرور را بی‌معنا می‌کنند.
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
    # IP های عمومیِ حل‌شده‌ی دامنه (آدرس‌های خصوصی/رزرو‌شده فیلتر می‌شوند)
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

# -- خواندن و نوشتن .env ------------------------------------------------------
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
    # env_set <فایل> <کلید> <مقدار> — جایگزینی کل خط یا افزودن آن
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
    # ویرایش اتمیک: نوشتن در فایل موقت، سپس mv؛ با پشتیبان زمان‌دار.
    local new_domain="$1" email="$2" stamp backup tmp
    stamp="$(date +%Y%m%d-%H%M%S)"
    backup=".env.bak.${stamp}"

    cp -p .env "$backup" || { err "گرفتن پشتیبان از .env ناموفق بود."; return 1; }
    ok "پشتیبان .env ذخیره شد: ${backup}"

    tmp="$(mktemp .env.tmp.XXXXXX)" || { err "ساخت فایل موقت ناموفق بود."; return 1; }
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
    ok "فایل .env به‌روزرسانی شد (بقیه‌ی خطوط و کامنت‌ها دست‌نخورده)."
    return 0
}

wait_for_url() {
    local url="$1" timeout="${2:-$HTTPS_TIMEOUT}" waited=0
    step "انتظار برای پاسخ ${url} (تا ${timeout} ثانیه)"
    if [ "${url#https://}" != "$url" ]; then
        dim "  اولین درخواست ممکن است چند ثانیه طول بکشد تا گواهی SSL صادر شود."
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
            dim "  … ${waited} ثانیه"
        fi
    done
    return 1
}

cert_file_path() {
    # مسیر فایل گواهی داخل حجم caddy_data
    $COMPOSE --profile tls exec -T caddy sh -c \
        "find /data/caddy/certificates -name '${1}.crt' 2>/dev/null | head -n1" 2>/dev/null | tr -d '\r' || true
}

cert_details() {
    # خلاصه‌ی گواهی: subject/issuer/expiry + روزهای باقی‌مانده
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

    printf '%s\n' "${C_BOLD}WG-Guard Bot — وضعیت دامنه و گواهی${C_RESET}"
    say ""

    if [ -z "$domain" ]; then
        info "دامنه: تنظیم نشده (حالت بدون SSL)"
        say "   حالت ربات      : ${mode}"
        say "   آدرس پنل       : $(env_get .env PANEL_BASE_URL)"
        say "   Caddy          : اجرا نمی‌شود (پروفایل tls غیرفعال است)"
        say ""
        dim "برای فعال‌کردن دامنه و SSL خودکار:  bash scripts/set-domain.sh example.com"
        return 0
    fi

    info "دامنه: ${domain}"
    say "   ایمیل ACME     : ${email:-(ثبت نشده — هشدار انقضا نمی‌آید)}"
    say "   حالت ربات      : ${mode}"
    say "   آدرس پنل       : $(env_get .env PANEL_BASE_URL)"
    say "   اتصال پنل روی هاست: ${bind}:$(env_get .env PANEL_PORT)"
    say ""

    # -- وضعیت کانتینر Caddy --------------------------------------------------
    if running_services | grep -q '^caddy$'; then
        caddy_state="$($COMPOSE --profile tls ps caddy --format '{{.Status}}' 2>/dev/null | head -n1 || true)"
        ok "Caddy در حال اجراست ${caddy_state:+(${caddy_state})}"
    else
        warn "Caddy در حال اجرا نیست."
        dim "  اجرا:  $COMPOSE --profile tls up -d"
    fi

    # -- گواهی -----------------------------------------------------------------
    if cert_details "$domain"; then
        say ""
        say "   صادرکننده (issuer) : ${CERT_ISSUER:-نامشخص}"
        say "   موضوع (subject)    : ${CERT_SUBJECT:-نامشخص}"
        say "   تاریخ انقضا        : ${CERT_END:-نامشخص}"
        if [ -n "$CERT_DAYS" ]; then
            say "   روزهای باقی‌مانده   : ${CERT_DAYS}"
        fi
        ok "گواهی SSL موجود است و ${C_GREEN}خودکار تمدید می‌شود${C_RESET} (حدود ۳۰ روز قبل از انقضا)."
        info "نیازی به certbot، cron یا تمدید دستی نیست."
    else
        warn "گواهی SSL هنوز در حجم caddy_data پیدا نشد."
        dim "  اگر تازه دامنه را تنظیم کرده‌اید، اولین درخواست HTTPS آن را می‌سازد:"
        dim "      curl -I https://${domain}/healthz"
        dim "  و اگر خطا داد:  $COMPOSE --profile tls logs --tail=40 caddy"
    fi

    # -- بررسی سلامت عمومی -----------------------------------------------------
    if command -v curl >/dev/null 2>&1; then
        say ""
        if curl -fsS --max-time 10 "https://${domain}/healthz" >/dev/null 2>&1; then
            https_ok="true"
        fi
        if [ "$https_ok" = "true" ]; then
            ok "بررسی سلامت روی https://${domain}/healthz موفق بود."
        else
            warn "پاسخ سالمی از https://${domain}/healthz نیامد (DNS، پورت ۸۰/۴۴۳ یا صدور گواهی را بررسی کنید)."
        fi
    fi

    say ""
    info "برای تغییر دامنه:      bash scripts/set-domain.sh new.example.com"
    info "برای حذف دامنه:        bash scripts/set-domain.sh --disable"
    return 0
}

# ===========================================================================
#  --disable
# ===========================================================================
disable_domain() {
    local current
    current="$(normalize_domain "$(env_get .env DOMAIN)")"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — حذف دامنه${C_RESET}"
    say ""
    if [ -z "$current" ]; then
        info "دامنه‌ای تنظیم نشده بود؛ چیزی برای حذف نیست."
        return 0
    fi
    say "  دامنه‌ی فعلی: ${C_CYAN}${current}${C_RESET}"
    say "  با حذف دامنه، ربات به حالت polling و پنل به http://IP:PORT برمی‌گردد."
    say "  ${C_BOLD}حجم‌های داده و گواهی دست‌نخورده می‌مانند.${C_RESET}"
    say ""
    if ! confirm "دامنه حذف شود و ربات به حالت بدون دامنه برگردد؟" "y"; then
        info "لغو شد؛ هیچ تغییری اعمال نشد."
        return 0
    fi

    apply_env_updates "" ""
    step "راه‌اندازی دوباره‌ی سرویس‌ها (بدون Caddy)"
    $COMPOSE up -d --remove-orphans
    # کانتینر Caddy دیگر در پروفایل tls نیست؛ صریحاً هم پاکش می‌کنیم.
    $COMPOSE --profile tls rm -sf caddy >/dev/null 2>&1 || true
    ok "سرویس‌ها در حالت بدون دامنه اجرا شدند."

    local port
    port="$(env_get .env PANEL_PORT)"
    [ -n "$port" ] || port="8080"
    say ""
    info "پنل روی این آدرس در دسترس است: http://${PUBLIC_IP}:${port}/panel/login"
    dim "  ربات چند ثانیه فرصت می‌خواهد تا بالا بیاید؛ اگر باز نشد:"
    dim "      $COMPOSE logs --tail=40 bot"
    dim "گواهی قبلی در حجم caddy_data باقی است اما دیگر استفاده نمی‌شود."
    return 0
}

# ===========================================================================
#  --renew
# ===========================================================================
renew_now() {
    local domain
    domain="$(normalize_domain "$(env_get .env DOMAIN)")"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — تمدید گواهی${C_RESET}"
    say ""
    if [ -z "$domain" ]; then
        err "دامنه‌ای تنظیم نشده است؛ اول دامنه را تنظیم کنید:"
        dim "    bash scripts/set-domain.sh example.com"
        return 1
    fi

    info "Caddy گواهی SSL را ${C_GREEN}خودکار${C_RESET} تمدید می‌کند — حدود ۳۰ روز قبل از انقضا."
    dim "  این دستور فقط تنظیمات Caddy را دوباره بارگذاری می‌کند و تاریخ انقضا را نشان می‌دهد؛"
    dim "  گواهی‌ها پاک نمی‌شوند (این کار به Let's Encrypt فشار می‌آورد و ممکن است محدود شوید)."
    say ""

    step "بارگذاری دوباره‌ی تنظیمات Caddy"
    if $COMPOSE --profile tls exec -T caddy caddy reload --config /etc/caddy/Caddyfile; then
        ok "تنظیمات Caddy دوباره بارگذاری شد (بدون قطع سرویس)."
    else
        warn "بارگذاری دوباره ناموفق بود؛ Caddy در حال اجراست؟"
        dim "  وضعیت:  $COMPOSE --profile tls ps caddy"
        dim "  لاگ:    $COMPOSE --profile tls logs --tail=40 caddy"
        return 1
    fi

    say ""
    if cert_details "$domain"; then
        info "وضعیت گواهی «${domain}»:"
        say "   صادرکننده : ${CERT_ISSUER:-نامشخص}"
        say "   انقضا     : ${CERT_END:-نامشخص}"
        [ -n "$CERT_DAYS" ] && say "   روزهای باقی‌مانده: ${CERT_DAYS}"
        ok "تمدید خودکار فعال است؛ کار دیگری لازم نیست."
    else
        warn "گواهی پیدا نشد. برای صدور اولین گواهی، یک درخواست HTTPS بفرستید:"
        dim "    curl -I https://${domain}/healthz"
    fi
    return 0
}

# ===========================================================================
#  تعیین/تغییر دامنه
# ===========================================================================
set_domain() {
    local new_domain="$1" email="" current="" dns_ok="false" resolved=""

    new_domain="$(normalize_domain "$new_domain")"

    if [ -z "$new_domain" ]; then
        err "دامنه خالی است. برای حذف دامنه از --disable استفاده کنید."
        return 2
    fi
    if is_ip_address "$new_domain"; then
        err "«${new_domain}» یک IP است؛ Let's Encrypt برای IP گواهی صادر نمی‌کند."
        say "  برای اجرای بدون دامنه:  bash scripts/set-domain.sh --disable"
        return 2
    fi
    if [ "$new_domain" = "localhost" ] || [ "${new_domain#*.}" = "local" ]; then
        err "«${new_domain}» دامنه‌ی عمومی نیست؛ Let's Encrypt برای آن گواهی صادر نمی‌کند."
        return 2
    fi
    if ! validate_domain "$new_domain"; then
        err "دامنه‌ی «${new_domain}» معتبر نیست. قالب درست: bot.example.com"
        return 2
    fi

    printf '%s\n' "${C_BOLD}WG-Guard Bot — تعیین دامنه${C_RESET}"
    say ""

    detect_public_ip
    current="$(normalize_domain "$(env_get .env DOMAIN)")"
    if [ -n "$current" ] && [ "$current" != "$new_domain" ]; then
        warn "دامنه از «${current}» به «${new_domain}» تغییر می‌کند."
        say "   • وبهوک تلگرام در شروع بعدی ربات خودکار روی دامنه‌ی جدید ثبت می‌شود."
        say "   • گواهی دامنه‌ی قبلی در حجم caddy_data می‌ماند اما دیگر استفاده نمی‌شود."
        say ""
        if ! confirm "ادامه می‌دهیم؟" "y"; then
            info "لغو شد؛ هیچ تغییری اعمال نشد."
            return 0
        fi
    fi

    # -- ایمیل ACME ------------------------------------------------------------
    email="$(env_get .env ACME_EMAIL)"
    if [ "$ASSUME_YES" != "true" ]; then
        printf '%s? %s%s %s[%s]%s\n  %s❯%s ' \
            "$C_BOLD" "ایمیل برای Let's Encrypt (اختیاری)" "$C_RESET" \
            "$C_DIM" "${email:-(خالی)}" "$C_RESET" "$C_GREEN" "$C_RESET" >&2
        local answer=""
        IFS= read -r answer || answer=""
        if [ -n "$answer" ]; then
            email="$answer"
        fi
    fi
    email="$(printf '%s' "$email" | tr -d '[:space:]')"
    if [ -z "$email" ]; then
        warn "ایمیلی ثبت نشد؛ گواهی صادر می‌شود اما هشدار انقضا نمی‌آید."
    fi

    # -- بررسی DNS -------------------------------------------------------------
    step "بررسی DNS دامنه‌ی ${new_domain}"
    say "   IP این سرور: ${PUBLIC_IP}"
    resolved="$(resolve_domain_ips "$new_domain" | tr '\n' ' ' | sed -e 's/[[:space:]]*$//')"
    say "   IP دامنه   : ${resolved:-پیدا نشد}"
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
        ok "دامنه درست به این سرور اشاره می‌کند."
    else
        warn "دامنه هنوز به این سرور اشاره نمی‌کند."
        dim "  یک رکورد A بسازید:  ${new_domain}  A  ${PUBLIC_IP}"
        if [ "$ASSUME_YES" != "true" ]; then
            if ! confirm "با این وضعیت ادامه می‌دهیم؟ (گواهی تا درست‌شدن DNS صادر نمی‌شود)" "n"; then
                info "لغو شد. بعد از درست‌کردن DNS دوباره اجرا کنید:"
                dim "    bash scripts/set-domain.sh ${new_domain}"
                return 0
            fi
        else
            warn "حالت --yes: بدون تأیید ادامه می‌دهیم."
        fi
    fi

    # -- نوشتن .env و بالا آوردن سرویس‌ها --------------------------------------
    step "به‌روزرسانی فایل .env"
    apply_env_updates "$new_domain" "$email"

    step "راه‌اندازی سرویس‌ها با Caddy (SSL خودکار)"
    $COMPOSE --profile tls up -d --remove-orphans
    ok "سرویس‌ها اجرا شدند."

    say ""
    if wait_for_url "https://${new_domain}/healthz"; then
        ok "پنل روی HTTPS در دسترس است: https://${new_domain}/panel/login"
        if cert_details "$new_domain"; then
            say ""
            say "   صادرکننده : ${CERT_ISSUER:-نامشخص}"
            say "   انقضا     : ${CERT_END:-نامشخص}"
            [ -n "$CERT_DAYS" ] && say "   روزهای باقی‌مانده: ${CERT_DAYS}"
        fi
        say ""
        ok "گواهی SSL ${C_GREEN}خودکار تمدید می‌شود${C_RESET}؛ نیازی به certbot یا cron نیست."
    else
        say ""
        err "پنل روی HTTPS پاسخ نداد؛ صدور گواهی کامل نشده است."
        say ""
        printf '%s── ۴۰ خط آخر لاگ Caddy ───────────────────────────%s\n' "$C_BOLD" "$C_RESET"
        $COMPOSE --profile tls logs --tail=40 caddy 2>&1 || true
        say ""
        info "چک‌لیست:"
        say "  ۱) رکورد A دامنه به ${PUBLIC_IP} اشاره می‌کند؟  dig +short ${new_domain}"
        say "  ۲) پورت ۸۰ و ۴۴۳ باز هستند؟  ufw allow 80/tcp && ufw allow 443/tcp"
        say "  ۳) محدودیت نرخ Let's Encrypt؟ (چند تلاش پشت‌سرهم = حدود یک ساعت صبر)"
        say "  ۴) CDN/پروکسی مثل Cloudflare روی حالت DNS only باشد."
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
            --*)        err "گزینه‌ی ناشناخته: $1"; say ""; usage; exit 2 ;;
            *)          DOMAIN_ARG="$1"; MODE="set" ;;
        esac
        shift
    done

    if [ -z "$MODE" ]; then
        usage
        exit 2
    fi

    cd "$PROJECT_DIR" || { err "پوشه‌ی پروژه پیدا نشد: $PROJECT_DIR"; exit 1; }
    if [ ! -f .env ]; then
        err "فایل .env در «${PROJECT_DIR}» پیدا نشد؛ اول نصب کنید: bash install.sh"
        exit 1
    fi
    if [ ! -f docker-compose.yml ]; then
        err "فایل docker-compose.yml پیدا نشد؛ این اسکریپت را از داخل پوشه‌ی پروژه اجرا کنید."
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
