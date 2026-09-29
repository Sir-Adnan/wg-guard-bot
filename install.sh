#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — نصب‌کننده‌ی خودکار برای سرورهای تازه‌ی Ubuntu / Debian
#  WG-Guard Bot — one-command installer for a fresh Ubuntu/Debian VPS.
#
#  اجرای سریع / quick start:
#      bash <(curl -fsSL https://raw.githubusercontent.com/Sir-Adnan/wg-guard-bot/main/install.sh)
#
#  این اسکریپت: داکر را (در صورت نبود) نصب می‌کند، فایل .env را با کلیدها و
#  رمزهای تصادفی می‌سازد، اطلاعات ربات را می‌پرسد، سرویس‌ها را بالا می‌آورد و
#  تا آماده‌شدن پنل صبر می‌کند.
# ---------------------------------------------------------------------------
set -euo pipefail

# ===========================================================================
#  پیکربندی پیش‌فرض / defaults
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

# مقادیر مشترک که در جریان اجرا پر می‌شوند (تعریف اولیه برای سازگاری با set -u)
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
#  رنگ‌ها / colours — فقط وقتی خروجی یک ترمینال باشد و NO_COLOR تنظیم نشده باشد
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
#  توابع کمکی چاپ / output helpers
# ===========================================================================
say()  { printf '%s\n' "$*"; }
info() { printf '%s%s%s\n' "$C_CYAN" "$*" "$C_RESET"; }
ok()   { printf '%s✔%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn() { printf '%s⚠%s  %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
err()  { printf '%s✖%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
step() { printf '\n%s%s▸ %s%s\n' "$C_BOLD" "$C_BLUE" "$*" "$C_RESET"; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

# ---------------------------------------------------------------------------
#  نسخه / version — از pyproject.toml خوانده می‌شود تا هرگز با
#  نسخه‌ی واقعی اختلاف پیدا نکند. اگر فایل در دسترس نباشد
#  (اجرای مستقیم با curl)، نسخه چاپ نمی‌شود.
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
        printf '%s\n' "   نصب‌کننده‌ی خودکار — نسخه‌ی ${C_BOLD}${APP_VERSION}${C_RESET}"
    fi
    printf '%s\n\n' "   ${C_DIM}ربات فروش VPN روی پنل WG-Guard / AmneziaWG${C_RESET}"
}

# ===========================================================================
#  راهنما / help
# ===========================================================================
usage() {
    cat <<EOF
${C_BOLD}WG-Guard Bot — راهنمای نصب${C_RESET}

${C_BOLD}روش سریع (توصیه‌شده):${C_RESET}
  bash <(curl -fsSL https://raw.githubusercontent.com/Sir-Adnan/wg-guard-bot/main/install.sh)

${C_BOLD}روش دستی:${C_RESET}
  git clone https://github.com/Sir-Adnan/wg-guard-bot.git wg-guard-bot
  cd wg-guard-bot && bash install.sh

${C_BOLD}گزینه‌ها / گزینه‌های خط فرمان:${C_RESET}
  ${C_CYAN}--yes${C_RESET}                 حالت غیرتعاملی؛ همه‌ی مقادیر پیش‌فرض یا از فلگ‌ها گرفته می‌شوند
                          (در این حالت ${C_BOLD}--bot-token${C_RESET} و ${C_BOLD}--admin-ids${C_RESET} اجباری‌اند)
  ${C_CYAN}--bot-token TOKEN${C_RESET}     توکن ربات از @BotFather
  ${C_CYAN}--admin-ids IDS${C_RESET}       شناسه‌های عددی مدیران، با کاما: 111,222
  ${C_CYAN}--support-ids IDS${C_RESET}     شناسه‌های عددی پشتیبان‌ها (اختیاری، با کاما)
  ${C_CYAN}--port PORT${C_RESET}           پورت پنل مدیریت (پیش‌فرض: ${PANEL_PORT_DEFAULT})
  ${C_CYAN}--panel-url URL${C_RESET}       آدرس عمومی پنل (پیش‌فرض: http://IP:PORT)
  ${C_CYAN}--owner-username NAME${C_RESET} نام کاربری مالک پنل (پیش‌فرض: ${OWNER_USERNAME_DEFAULT})
  ${C_CYAN}--owner-password PASS${C_RESET} رمز مالک پنل (پیش‌فرض: خودکار و خوانا)
  ${C_CYAN}--app-name NAME${C_RESET}       نام نمایشی فروشگاه (پیش‌فرض: ${APP_NAME_DEFAULT})
  ${C_CYAN}--domain NAME${C_RESET}         دامنه یا زیردامنه برای SSL خودکار، مثال: bot.example.com
                          (دامنه خودکار https و حالت webhook را فعال می‌کند)
  ${C_CYAN}--acme-email MAIL${C_RESET}     ایمیل Let's Encrypt برای هشدار انقضای گواهی (اختیاری)
  ${C_CYAN}--no-domain${C_RESET}           بدون دامنه و بدون SSL (حالت polling روی http://IP:PORT)
  ${C_CYAN}--ip ADDRESS${C_RESET}          تعیین دستی IP عمومی سرور (پیش‌فرض: تشخیص خودکار)
  ${C_CYAN}--dir PATH${C_RESET}            مسیر نصب/مخزن (پیش‌فرض: پوشه‌ی فعلی یا ~/wg-guard-bot)
  ${C_CYAN}--branch NAME${C_RESET}         شاخه‌ی مخزن برای نصب و به‌روزرسانی (پیش‌فرض: main)
  ${C_CYAN}--force${C_RESET}               بازنویسی .env موجود (با نسخه‌ی پشتیبان) حتی در حالت --yes
  ${C_CYAN}--no-docker-install${C_RESET}   اگر داکر نصب نیست، نصبش نکن (نیاز به دسترسی root دارد)
  ${C_CYAN}-h, --help${C_RESET}            نمایش همین راهنما

${C_BOLD}نمونه‌ی نصب کاملاً خودکار:${C_RESET}
  bash install.sh --yes \\
      --bot-token 123456789:AA... \\
      --admin-ids 111111111 \\
      --port 8080

${C_BOLD}نکته‌ها:${C_RESET}
  • این اسکریپت به دسترسی root نیاز دارد (در صورت نبود، خودش با sudo اجرا می‌شود).
  • اجرای دوباره‌ی اسکریپت بی‌خطر است؛ فایل .env قبلی پشتیبان‌گیری می‌شود و
    رمزهای موجود دست‌نخورده می‌مانند.

${C_DIM}English: one-command installer for WG-Guard Bot (Docker Compose stack) on a
fresh Ubuntu/Debian VPS — generates .env, builds the image, starts the stack
and waits for the panel health endpoint.${C_RESET}
EOF
}

# ===========================================================================
#  تجزیه‌ی آرگومان‌ها / argument parsing
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
                err "گزینه‌ی ناشناخته: $1"
                say ""
                dim "برای دیدن راهنما اجرا کنید: bash install.sh --help"
                exit 2
                ;;
            *)
                err "آرگومان اضافی و ناشناخته: $1"
                exit 2
                ;;
        esac
        shift
    done
}

# ===========================================================================
#  بررسی‌های پایه / preflight
# ===========================================================================
require_root() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        warn "این اسکریپت به دسترسی root نیاز دارد؛ همین حالا با sudo دوباره اجرا می‌شود…"
        exec sudo -E bash "$0" "$@"
    fi
    err "برای نصب به دسترسی root نیاز است و sudo هم نصب نیست."
    say "  لطفاً با کاربر root وارد شوید یا sudo را نصب کنید: apt-get install -y sudo"
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
        warn "نسخه‌ی قدیمی docker-compose (v1) نصب است؛ این اسکریپت به Docker Compose v2 نیاز دارد."
        say "  نصب: apt-get update && apt-get install -y docker-compose-plugin"
    fi
    COMPOSE=""
    return 1
}

install_docker() {
    if command -v docker >/dev/null 2>&1; then
        return 0
    fi
    if [ "$SKIP_DOCKER" = "true" ]; then
        err "داکر نصب نیست و گزینه‌ی --no-docker-install هم فعال است."
        say "  یا داکر را خودتان نصب کنید، یا اسکریپت را بدون آن فلگ اجرا کنید."
        exit 1
    fi
    step "داکر نصب نیست؛ در حال نصب Docker از اسکریپت رسمی…"
    dim "  (چند دقیقه طول می‌کشد و به اینترنت نیاز دارد)"
    if ! command -v curl >/dev/null 2>&1; then
        warn "curl نصب نیست؛ همین حالا نصب می‌شود…"
        if command -v apt-get >/dev/null 2>&1; then
            apt-get update -qq >/dev/null 2>&1 || true
            apt-get install -y -qq curl >/dev/null 2>&1 || true
        fi
    fi
    if ! command -v curl >/dev/null 2>&1; then
        err "curl نصب نیست و نصب خودکار آن هم ناموفق بود."
        say "  نصب دستی: apt-get install -y curl"
        exit 1
    fi
    if ! curl -fsSL https://get.docker.com -o /tmp/get-docker.sh; then
        err "دانلود اسکریپت نصب داکر ناموفق بود."
        say "  اتصال اینترنت سرور را بررسی کنید و دوباره تلاش کنید."
        exit 1
    fi
    if ! sh /tmp/get-docker.sh; then
        err "نصب داکر ناموفق بود (اسکریپت رسمی با خطا خارج شد)."
        say "  می‌توانید دستی نصب کنید: https://docs.docker.com/engine/install/"
        rm -f /tmp/get-docker.sh
        exit 1
    fi
    rm -f /tmp/get-docker.sh
    ok "داکر با موفقیت نصب شد."
    if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
        warn "داکر نصب شد اما پلاگین compose پیدا نشد؛ تلاش برای نصب آن…"
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
#  پرسیدن مقدار از کاربر / interactive prompt
# ===========================================================================
# ask <برچسب> <پیش‌فرض> [توضیح]
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

# ask_required <برچسب> <پیش‌فرض> <توضیح> <الگوی اعتبارسنجی> <پیام خطا>
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
    # confirm <پرسش> [پیش‌فرض y/n]
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
#  تولید رمزها / secret generation
# ===========================================================================
gen_hex() {
    # ۶۴ کاراکتر هگز برای SECRET_KEY
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex 32
        return 0
    fi
    head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-64
}

gen_password() {
    # رمز ۲۴ کاراکتری برای POSTGRES_PASSWORD (بدون کاراکترهای مشکل‌دار برای .env و psql)
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 24 | tr -d '/+=' | cut -c1-24
        return 0
    fi
    head -c 32 /dev/urandom | base64 | tr -d '/+=' | cut -c1-24
}

gen_readable_password() {
    # رمز ۱۶ کاراکتری خوانا برای مالک پنل — بدون کاراکترهای گیج‌کننده (0/O، 1/l/I)
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
    # مسیر مخفی وب‌هوک — فقط حروف و اعداد (در URL استفاده می‌شود)
    local out=""
    if command -v openssl >/dev/null 2>&1; then
        out="$(openssl rand -hex 16)"
    else
        out="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-32)"
    fi
    printf '%s' "$out"
}

# ===========================================================================
#  خواندن مقدار فعلی از .env / read an existing value
# ===========================================================================
env_get() {
    # env_get <فایل> <کلید> — مقدار بدون کوتیشن را چاپ می‌کند
    local file="$1" key="$2" line=""
    [ -f "$file" ] || return 0
    line="$(grep -E "^[[:space:]]*${key}=" "$file" | tail -n1 || true)"
    [ -n "$line" ] || return 0
    line="${line#*=}"
    line="$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    # حذف کوتیشن‌های ابتدا و انتها
    case "$line" in
        \"*\") line="${line#\"}"; line="${line%\"}" ;;
        \'*\') line="${line#\'}"; line="${line%\'}" ;;
    esac
    printf '%s' "$line"
}

env_set() {
    # env_set <فایل> <کلید> <مقدار> — جایگزینی کل خط یا افزودن آن
    local file="$1" key="$2" value="$3"
    if grep -Eq "^[[:space:]]*${key}=" "$file"; then
        # از | به‌عنوان جداکننده استفاده می‌کنیم و کاراکترهای خاص sed را امن می‌کنیم
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
    # فقط اعداد و کاما؛ حداقل یک شناسه
    printf '%s' "$1" | grep -Eq '^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$'
}

# ===========================================================================
#  دامنه و SSL خودکار / domain + automatic TLS (Caddy)
# ===========================================================================
normalize_domain() {
    # حذف طرح (http/https)، مسیر و اسلش انتهایی؛ تبدیل به حروف کوچک
    printf '%s' "$1" \
        | tr '[:upper:]' '[:lower:]' \
        | sed -e 's|^[[:space:]]*||' -e 's|[[:space:]]*$||' \
              -e 's|^[a-z][a-z0-9+.-]*://||' \
              -e 's|/.*$||' \
              -e 's|^\.*||' -e 's|\.*$||'
}

validate_domain() {
    # دامنه‌ی معتبر (حداقل دو بخش)، بدون IP و بدون localhost
    printf '%s' "$1" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
}

is_ip_address() {
    # IPv4 ساده یا هر چیزی که فقط رقم و نقطه است
    printf '%s' "$1" | grep -Eq '^[0-9]{1,3}(\.[0-9]{1,3}){3}$'
}

is_private_ip() {
    # آدرس‌های خصوصی/رزرو‌شده — برای مقایسه‌ی DNS بی‌فایده‌اند و نشانه‌ی
    # DNS hijack یا رزولور محلی هستند، نه یک رکورد A واقعی.
    local ip="$1" a b
    case "$ip" in
        10.*|127.*|169.254.*|192.168.*|0.*) return 0 ;;
    esac
    a="$(printf '%s' "$ip" | cut -d. -f1)"
    b="$(printf '%s' "$ip" | cut -d. -f2)"
    if [ "$a" = "172" ] && [ "$b" -ge 16 ] 2>/dev/null && [ "$b" -le 31 ] 2>/dev/null; then
        return 0
    fi
    # بازه‌های مستندسازی RFC 5737
    case "$ip" in
        192.0.2.*|198.51.100.*|203.0.113.*) return 0 ;;
    esac
    return 1
}

is_email() {
    printf '%s' "$1" | grep -Eq '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'
}

resolve_domain_ips() {
    # چاپ IP های عمومیِ حل‌شده‌ی دامنه (هر کدام در یک خط)
    # آدرس‌های خصوصی/رزرو‌شده فیلتر می‌شوند تا رزولور محلی یا DNS hijack
    # باعث هشدار اشتباه «دامنه به این سرور اشاره می‌کند» نشود.
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
    # ۰ = دامنه به همین سرور اشاره می‌کند، ۱ = اشاره نمی‌کند یا حل نشد
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
    warn "دامنه‌ی «${DOMAIN_FINAL}» هنوز به این سرور اشاره نمی‌کند."
    say "   IP این سرور      : ${PUBLIC_IP}"
    say "   IP دامنه در DNS  : ${RESOLVED_IPS:-پیدا نشد}"
    say ""
    dim "  برای گرفتن گواهی SSL، یک رکورد A از دامنه به IP سرور بسازید:"
    dim "      ${DOMAIN_FINAL}  A  ${PUBLIC_IP}"
    dim "  (پروپاگیت DNS معمولاً چند دقیقه طول می‌کشد)"
    say ""
}

cert_status() {
    # خواندن گواهی از داخل حجم caddy_data و چاپ خلاصه‌ی آن
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
#  آماده‌سازی پوشه‌ی پروژه / project directory
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
        ok "پوشه‌ی پروژه پیدا شد: $INSTALL_DIR"
    else
        step "دریافت کد پروژه در $INSTALL_DIR"
        if [ -e "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null || true)" ]; then
            err "پوشه‌ی «$INSTALL_DIR» خالی نیست و فایل docker-compose.yml هم ندارد."
            say "  یک مسیر خالی انتخاب کنید (--dir) یا پروژه را دستی clone کنید:"
            dim "    git clone --branch $BRANCH ${REPO_URL%.git}.git \"$INSTALL_DIR\""
            exit 1
        fi
        if ! command -v git >/dev/null 2>&1; then
            err "برای دریافت کد، git لازم است و نصب نیست."
            say "  نصب: apt-get update && apt-get install -y git"
            exit 1
        fi
        if ! git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$INSTALL_DIR"; then
            err "دریافت کد از مخزن ناموفق بود: $REPO_URL"
            say "  اگر مخزن شما جای دیگری است، آدرس درست را بدهید:"
            dim "    WGGB_REPO_URL=https://github.com/Sir-Adnan/wg-guard-bot.git bash install.sh --branch $BRANCH"
            say "  یا پروژه را دستی clone کنید و install.sh را از داخل همان پوشه اجرا کنید."
            exit 1
        fi
        ok "کد پروژه دریافت شد."
    fi

    cd "$INSTALL_DIR"
    if [ ! -f "./.env.example" ]; then
        err "فایل .env.example در «$INSTALL_DIR» پیدا نشد؛ به‌نظر نمی‌رسد پوشه‌ی درستی باشد."
        exit 1
    fi
}

# ===========================================================================
#  تشخیص مقادیر قبلی .env / reuse existing installation
# ===========================================================================
load_existing() {
    # مقادیر قبلی؛ برای سازگاری با set -u همه از ابتدا تعریف می‌شوند
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
    warn "یک فایل .env از نصب قبلی پیدا شد."
    dim "  رمزها و کلیدهای موجود دست‌نخورده می‌مانند تا دیتابیس و پنل نشکند."

    if [ "$ASSUME_YES" != "true" ]; then
        if ! confirm "می‌خواهید فایل .env را با تنظیمات جدید بازسازی کنید؟ (پشتیبان گرفته می‌شود)" "n"; then
            REUSE_ENV="true"
            ok "از همان .env قبلی استفاده می‌شود."
            return 0
        fi
    else
        if [ "$FORCE" != "true" ]; then
            REUSE_ENV="true"
            ok "حالت --yes: فایل .env موجود دست‌نخورده ماند (برای بازنویسی: --force)."
            return 0
        fi
        warn "گزینه‌ی --force فعال است؛ فایل .env بازنویسی می‌شود (رمزهای قدیمی حفظ می‌شوند)."
    fi

    ENV_BACKUP=".env.bak.$(date +%Y%m%d-%H%M%S)"
    cp -p .env "$ENV_BACKUP"
    ok "پشتیبان گرفته شد: $ENV_BACKUP"
}

# ===========================================================================
#  ساخت .env / build the .env file
# ===========================================================================
write_env() {
    local target secret_key pg_password webhook_secret
    local bot_token admin_ids support_ids panel_port panel_url
    local owner_username owner_password app_name secret_len
    local domain_input acme_input panel_bind domain_error
    local DOMAIN_FROM_PROMPT=""
    local DOMAIN_VALUE="" ACME_EMAIL_VALUE="" PANEL_BIND_VALUE=""

    step "ساخت فایل تنظیمات .env"

    # -- رمزها: اگر نصب قبلی داشته باشیم، همان‌ها را نگه می‌داریم -------------
    secret_key="${OLD_SECRET_KEY:-}"
    pg_password="${OLD_POSTGRES_PASSWORD:-}"
    webhook_secret="${OLD_WEBHOOK_SECRET:-}"

    [ -n "$secret_key" ] || secret_key="$(gen_hex)"
    [ -n "$pg_password" ] || pg_password="$(gen_password)"
    [ -n "$webhook_secret" ] || webhook_secret="$(gen_webhook_secret)"

    # -- پرسش‌های تعاملی ----------------------------------------------------
    bot_token="$OPT_BOT_TOKEN"
    admin_ids="$OPT_ADMIN_IDS"
    support_ids="$OPT_SUPPORT_IDS"
    panel_port="$OPT_PORT"
    panel_url="$OPT_PANEL_URL"
    owner_username="$OPT_OWNER_USERNAME"
    owner_password="$OPT_OWNER_PASSWORD"
    app_name="$OPT_APP_NAME"

    # مقادیر قبلی به‌عنوان پیش‌فرض
    [ -n "$bot_token" ]       || bot_token="${OLD_BOT_TOKEN:-}"
    [ -n "$admin_ids" ]       || admin_ids="${OLD_ADMIN_IDS:-}"
    [ -n "$support_ids" ]     || support_ids="${OLD_SUPPORT_IDS:-}"
    [ -n "$panel_port" ]      || panel_port="${OLD_PANEL_PORT:-}"
    [ -n "$panel_url" ]       || panel_url="${OLD_PANEL_URL:-}"
    [ -n "$owner_username" ]  || owner_username="${OLD_OWNER_USERNAME:-}"
    [ -n "$app_name" ]        || app_name="${OLD_APP_NAME:-}"

    if [ "$ASSUME_YES" = "true" ]; then
        # ---- حالت غیرتعاملی: اعتبارسنجی سخت و خطای سریع -------------------
        if [ -z "$bot_token" ]; then
            err "در حالت --yes باید توکن ربات را بدهید."
            say "  نمونه: bash install.sh --yes --bot-token 123456789:AA... --admin-ids 111111111"
            exit 2
        fi
        if ! printf '%s' "$bot_token" | grep -Eq '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$'; then
            err "قالب توکن ربات درست نیست. باید شبیه این باشد: 123456789:AAH... (از @BotFather)"
            exit 2
        fi
        if [ -z "$admin_ids" ]; then
            err "در حالت --yes باید حداقل یک شناسه‌ی مدیر بدهید."
            say "  نمونه: --admin-ids 111111111,222222222"
            exit 2
        fi
        if ! validate_ids "$admin_ids"; then
            err "شناسه‌های مدیر باید عدد و با کاما جدا شده باشند؛ مثال: 111111111,222222222"
            exit 2
        fi
        if [ -n "$support_ids" ] && ! validate_ids "$support_ids"; then
            err "شناسه‌های پشتیبان باید عدد و با کاما جدا شده باشند."
            exit 2
        fi
        [ -n "$panel_port" ] || panel_port="$PANEL_PORT_DEFAULT"
        if ! validate_port "$panel_port"; then
            err "پورت نامعتبر: $panel_port (باید بین ۱ تا ۶۵۵۳۵ باشد)"
            exit 2
        fi
        [ -n "$owner_username" ] || owner_username="$OWNER_USERNAME_DEFAULT"
        [ -n "$app_name" ]       || app_name="$APP_NAME_DEFAULT"
        if [ -z "$owner_password" ]; then
            owner_password="${OLD_OWNER_PASSWORD:-}"
            [ -n "$owner_password" ] || owner_password="$(gen_readable_password)"
        fi
    else
        # ---- حالت تعاملی ---------------------------------------------------
        say ""
        info "به چند سؤال کوتاه جواب بدهید. هر جا پیش‌فرضی در [ ] دیدید، فقط Enter بزنید."
        say ""

        if [ -z "$bot_token" ]; then
            bot_token="$(ask_required \
                "توکن ربات (از @BotFather)" "" \
                "در تلگرام به @BotFather پیام بدهید → /newbot → توکن را کپی کنید" \
                '^[0-9]{6,}:[A-Za-z0-9_-]{30,}$' \
                "توکن نامعتبر است. باید شبیه 123456789:AAH... باشد (حداقل ۳۰ کاراکتر بعد از دونقطه).")"
        else
            ok "توکن ربات از فلگ خوانده شد."
        fi

        if [ -z "$admin_ids" ]; then
            admin_ids="$(ask_required \
                "شناسه‌ی عددی مدیران (با کاما جدا کنید)" "" \
                "شناسه‌ی خودتان را از @userinfobot بگیرید. مثال: 111111111,222222222" \
                '^[0-9]+([[:space:]]*,[[:space:]]*[0-9]+)*$' \
                "حداقل یک شناسه‌ی عددی لازم است (فقط عدد و کاما).")"
        else
            ok "شناسه‌های مدیر از فلگ خوانده شد."
        fi

        support_ids="$(ask \
            "شناسه‌ی عددی پشتیبان‌ها (اختیاری، با کاما)" "$support_ids" \
            "پشتیبان‌ها دسترسی محدود دارند؛ اگر نمی‌خواهید، خالی بگذارید")"
        support_ids="$(printf '%s' "$support_ids" | tr -d '[:space:]')"
        if [ -n "$support_ids" ] && ! validate_ids "$support_ids"; then
            warn "شناسه‌های پشتیبان نامعتبر بود و نادیده گرفته شد."
            support_ids=""
        fi

        panel_port="$(ask "پورت پنل مدیریت" "${panel_port:-$PANEL_PORT_DEFAULT}" \
            "پورت HTTP پنل؛ اگر اشغال است عدد دیگری بدهید (مثلاً 8090)")"
        panel_port="$(printf '%s' "$panel_port" | tr -d '[:space:]')"
        while ! validate_port "$panel_port"; do
            err "پورت نامعتبر است؛ عددی بین ۱ تا ۶۵۵۳۵ وارد کنید."
            panel_port="$(ask "پورت پنل مدیریت" "$PANEL_PORT_DEFAULT" "")"
            panel_port="$(printf '%s' "$panel_port" | tr -d '[:space:]')"
        done

        if [ -z "$panel_url" ]; then
            panel_url="$(ask "آدرس عمومی پنل" "http://${PUBLIC_IP}:${panel_port}" \
                "اگر دامنه و SSL دارید بنویسید: https://bot.example.com")"
        fi
        panel_url="$(printf '%s' "$panel_url" | sed -e 's|[[:space:]]||g' -e 's|/*$||')"

        # -- دامنه و SSL (اختیاری) -------------------------------------------
        # خالی گذاشتن دامنه = همان رفتار قبلی: polling روی http://IP:PORT
        if [ "$NO_DOMAIN" != "true" ]; then
            domain_input="$(ask "دامنه یا زیردامنه (خالی = بدون دامنه و بدون SSL)" "" \
                "اگر دامنه دارید، اول یک رکورد A از آن به IP این سرور (${PUBLIC_IP}) بسازید")"
            DOMAIN_FROM_PROMPT="$(normalize_domain "$domain_input")"
        fi

        owner_username="$(ask "نام کاربری مالک پنل" "${owner_username:-$OWNER_USERNAME_DEFAULT}" \
            "با این نام کاربری وارد پنل می‌شوید")"
        owner_username="$(printf '%s' "$owner_username" | tr -d '[:space:]')"
        [ -n "$owner_username" ] || owner_username="$OWNER_USERNAME_DEFAULT"

        if [ -z "$owner_password" ]; then
            owner_password="${OLD_OWNER_PASSWORD:-}"
            if [ -z "$owner_password" ]; then
                owner_password="$(gen_readable_password)"
            fi
        fi

        app_name="$(ask "نام نمایشی فروشگاه" "${app_name:-$APP_NAME_DEFAULT}" \
            "همین نام در پیام‌های ربات و پنل دیده می‌شود")"
        [ -n "$app_name" ] || app_name="$APP_NAME_DEFAULT"
    fi

    # -- اعتبارسنجی دامنه (هر دو حالت) -------------------------------------
    domain_input="${OPT_DOMAIN:-}"
    [ -n "$domain_input" ] || domain_input="$DOMAIN_FROM_PROMPT"
    DOMAIN_VALUE="$(normalize_domain "$domain_input")"

    if [ "$NO_DOMAIN" = "true" ]; then
        DOMAIN_VALUE=""
    fi

    # در حالت تعاملی، دامنه‌ی نامعتبر را دوباره می‌پرسیم؛ در --yes خطا می‌دهیم.
    while [ -n "$DOMAIN_VALUE" ]; do
        domain_error=""
        if is_ip_address "$DOMAIN_VALUE"; then
            domain_error="برای IP نمی‌توان گواهی SSL گرفت؛ «${DOMAIN_VALUE}» یک IP است."
        elif [ "$DOMAIN_VALUE" = "localhost" ] || [ "${DOMAIN_VALUE#*.}" = "local" ]; then
            domain_error="«${DOMAIN_VALUE}» دامنه‌ی عمومی نیست و Let's Encrypt برایش گواهی صادر نمی‌کند."
        elif ! validate_domain "$DOMAIN_VALUE"; then
            domain_error="دامنه‌ی «${DOMAIN_VALUE}» معتبر نیست؛ قالب درست: bot.example.com"
        fi

        [ -n "$domain_error" ] || break

        err "$domain_error"
        if [ "$ASSUME_YES" = "true" ] || [ "$DOMAIN_FROM_PROMPT" = "" ]; then
            say "  در این حالت دامنه را خالی بگذارید تا ربات بدون SSL اجرا شود"
            say "  و پنل روی http://${PUBLIC_IP}:${PANEL_PORT_DEFAULT} در دسترس باشد."
            if [ "$ASSUME_YES" = "true" ]; then exit 2; fi
            DOMAIN_VALUE=""
            break
        fi
        say "  برای بدون‌دامنه‌بودن، فقط Enter بزنید."
        domain_input="$(ask "دامنه یا زیردامنه (خالی = بدون دامنه و بدون SSL)" "")"
        DOMAIN_VALUE="$(normalize_domain "$domain_input")"
    done

    # -- ایمیل Let's Encrypt (فقط وقتی دامنه داریم) --------------------------
    if [ -n "$DOMAIN_VALUE" ]; then
        if [ -n "$OPT_ACME_EMAIL" ]; then
            acme_input="$OPT_ACME_EMAIL"
        elif [ "$ASSUME_YES" = "true" ]; then
            acme_input="${OLD_ACME_EMAIL:-}"
        else
            acme_input="$(ask "ایمیل برای Let's Encrypt (اختیاری)" "${OLD_ACME_EMAIL:-}" \
                "برای اطلاع‌رسانی نزدیک‌شدن انقضای گواهی؛ خالی هم قابل قبول است")"
        fi
        acme_input="$(printf '%s' "$acme_input" | tr -d '[:space:]')"
        if [ -z "$acme_input" ]; then
            warn "ایمیلی برای Let's Encrypt ثبت نشد؛ گواهی صادر می‌شود اما هشدار انقضا نمی‌آید."
            dim "  (اگر بعداً خواستید، فقط ACME_EMAIL را در .env پر کنید و سرویس‌ها را دوباره بالا بیاورید)"
        elif ! is_email "$acme_input"; then
            warn "ایمیل «${acme_input}» معتبر به‌نظر نمی‌رسد و نادیده گرفته شد."
            acme_input=""
        fi
        ACME_EMAIL_VALUE="$acme_input"
    else
        ACME_EMAIL_VALUE=""
    fi

    # -- مقادیر نهایی دامنه/TLS --------------------------------------------
    if [ -n "$DOMAIN_VALUE" ]; then
        TLS_ENABLED="true"
        panel_url="https://${DOMAIN_VALUE}"
        panel_bind="$TLS_BIND"
        ok "دامنه ثبت شد: ${DOMAIN_VALUE} — SSL خودکار با Caddy فعال می‌شود."
    else
        TLS_ENABLED="false"
        panel_bind="$PANEL_BIND_DEFAULT"
        if [ -n "${OPT_DOMAIN:-}" ] || [ "${NO_DOMAIN:-false}" = "true" ]; then
            dim "  بدون دامنه: ربات در حالت polling و پنل روی http://${PUBLIC_IP}:${panel_port} اجرا می‌شود."
        fi
    fi
    PANEL_BIND_VALUE="$panel_bind"
    DOMAIN_VALUE_FINAL="$DOMAIN_VALUE"
    ACME_EMAIL_VALUE_FINAL="$ACME_EMAIL_VALUE"

    # -- نوشتن مقادیر در .env ----------------------------------------------
    # تا اینجا هیچ فایلی ساخته نشده: اگر اعتبارسنجی بالا شکست بخورد، نصب
    # نیمه‌کاره و .env ناقص روی سرور باقی نمی‌ماند.
    if [ -f .env ]; then
        # حالت --force یا بازسازی تعاملی: .env فعلی را سر جایش دست نمی‌زنیم
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

    # -- دامنه و SSL --------------------------------------------------------
    env_set "$target" DOMAIN "$DOMAIN_VALUE"
    env_set "$target" ACME_EMAIL "$ACME_EMAIL_VALUE"
    env_set "$target" PANEL_BIND "$PANEL_BIND_VALUE"
    if [ -n "$DOMAIN_VALUE" ]; then
        # حالت webhook: تلگرام باید بتواند روی HTTPS به ما وصل شود
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

    # پاک‌سازی متغیرهای حساس از حافظه‌ی محیطی این نشست
    OWNER_PASSWORD_SHOWN="$owner_password"
    OWNER_USERNAME_SHOWN="$owner_username"

    secret_len="${#secret_key}"
    ok "فایل .env ساخته شد (SECRET_KEY با ${secret_len} کاراکتر)."
    return 0
}

# ===========================================================================
#  بالا آوردن سرویس‌ها / bring the stack up
# ===========================================================================
compose_up() {
    step "دریافت ایمیج‌های پایه (Postgres و Redis و Caddy)"
    # در اولین اجرا ممکن است ایمیج اختصاصی ما در رجیستری نباشد؛ خطا را نادیده می‌گیریم.
    $COMPOSE_PROFILE pull --ignore-pull-failures 2>/dev/null || $COMPOSE_PROFILE pull || true
    ok "ایمیج‌های پایه آماده‌اند."

    if [ "$TLS_ENABLED" = "true" ]; then
        step "ساخت ایمیج ربات و اجرای سرویس‌ها + Caddy (SSL خودکار)"
    else
        step "ساخت ایمیج ربات و اجرای سرویس‌ها"
    fi
    dim "  (بار اول چند دقیقه طول می‌کشد؛ وابستگی‌های پایتون کامپایل می‌شوند)"
    if ! $COMPOSE_PROFILE up -d --build; then
        err "اجرای سرویس‌ها ناموفق بود."
        return 1
    fi
    ok "سرویس‌ها اجرا شدند."
    return 0
}

wait_for_health() {
    # wait_for_health <url> [timeout]
    local url="$1" timeout="${2:-$HEALTH_TIMEOUT_DEFAULT}" health_port
    local waited=0 interval=3

    health_port="$(printf '%s' "$url" | sed -e 's|^[a-z]*://||' -e 's|/.*$||' -e 's|:.*$||')"
    step "انتظار برای آماده‌شدن پنل (تا ${timeout} ثانیه)"
    dim "  آدرس بررسی سلامت: $url"
    if [ "$TLS_ENABLED" = "true" ]; then
        dim "  (اولین درخواست ممکن است چند ثانیه طول بکشد تا Caddy گواهی SSL را از Let's Encrypt بگیرد)"
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
            # بدون curl/wget: فقط وضعیت سرویس را بررسی می‌کنیم
            if $COMPOSE ps --status running 2>/dev/null | grep -q "bot"; then
                sleep 5
                return 0
            fi
        fi
        sleep "$interval"
        waited=$(( waited + interval ))
        if [ $(( waited % 30 )) -eq 0 ]; then
            dim "  … ${waited} ثانیه"
        fi
    done
    return 1
}

show_tls_failure_help() {
    say ""
    err "پنل روی HTTPS پاسخ نداد؛ یعنی صدور گواهی SSL کامل نشده است."
    say ""
    printf '%s── ۴۰ خط آخر لاگ Caddy ───────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE --profile tls logs --tail=40 caddy 2>&1 || true
    say ""
    printf '%s── ۲۰ خط آخر لاگ ربات ────────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE --profile tls logs --tail=20 bot 2>&1 || true
    say ""
    info "چک‌لیست رفع مشکل SSL (به ترتیب بررسی کنید):"
    say "  ۱) رکورد DNS: باید یک رکورد A از «${DOMAIN_FINAL}» به ${PUBLIC_IP} باشد."
    say "     بررسی:  dig +short ${DOMAIN_FINAL}"
    say "  ۲) پورت ۸۰ باید از اینترنت باز باشد؛ Let's Encrypt برای تأیید مالکیت دامنه"
    say "     به http://${DOMAIN_FINAL}/.well-known/acme-challenge/... وصل می‌شود."
    say "     بررسی:  ufw allow 80/tcp  و  ufw allow 443/tcp"
    say "  ۳) محدودیت نرخ Let's Encrypt: اگر چند بار پشت‌سرهم تلاش کرده‌اید،"
    say "     باید حدود یک ساعت صبر کنید (سقف هفتگی: ۵ گواهی برای هر دامنه)."
    say "  ۴) اگر پروکسی/CDN مثل Cloudflare دارید، آن را موقتاً روی حالت DNS only بگذارید."
    say ""
    dim "بعد از درست‌کردن مشکل، دوباره اجرا کنید:  bash install.sh   یا   bash scripts/set-domain.sh ${DOMAIN_FINAL}"
    say ""
}

show_failure_diagnostics() {
    say ""
    err "راه‌اندازی کامل نشد؛ اطلاعات زیر برای عیب‌یابی:"
    say ""
    printf '%s── وضعیت سرویس‌ها ─────────────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE ps 2>&1 | tail -n 20 || true
    say ""
    printf '%s── ۴۰ خط آخر لاگ ربات ────────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE logs --tail=40 bot 2>&1 || true
    say ""
    printf '%s── ۲۰ خط آخر لاگ دیتابیس ─────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    $COMPOSE logs --tail=20 db 2>&1 || true
    say ""
    info "کارهایی که معمولاً مشکل را حل می‌کنند:"
    say "  • توکن ربات درست است؟ (خطای 401 در لاگ یعنی توکن اشتباه است)"
    say "  • پورت ${PANEL_PORT_DEFAULT} آزاد است؟  ss -lntp | grep ${PANEL_PORT_DEFAULT}"
    say "  • فضای دیسک کافی است؟  df -h /"
    say "  • دوباره تلاش کنید:  $COMPOSE up -d --build"
    say ""
    dim "برای دیدن لاگ زنده:  $COMPOSE logs -f bot"
}

success_box() {
    local port="$1" url="$2" username="$3" password="$4" ip="$5"
    local line="════════════════════════════════════════════════════════════════"

    say ""
    printf '%s%s%s\n' "$C_GREEN" "$line" "$C_RESET"
    printf '%s%s  ✅  نصب با موفقیت تمام شد!%s\n' "$C_BOLD" "$C_GREEN" "$C_RESET"
    printf '%s%s%s\n' "$C_GREEN" "$line" "$C_RESET"
    say ""
    printf '  %s🌐 آدرس پنل مدیریت:%s\n' "$C_BOLD" "$C_RESET"
    printf '     %s%s/panel/login%s\n' "$C_CYAN" "$url" "$C_RESET"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '  %s🔒 گواهی SSL فعال است و %sخودکار تمدید می‌شود%s (حدود ۳۰ روز قبل از انقضا).%s\n' \
            "$C_BOLD" "$C_GREEN" "$C_RESET" "$C_RESET"
        if [ "$CERT_ISSUED" = "true" ] && [ -n "$CERT_DETAILS" ]; then
            printf '     %s%s%s\n' "$C_DIM" "$CERT_DETAILS" "$C_RESET"
        fi
        printf '     %sبرای دیدن وضعیت گواهی: bash scripts/set-domain.sh --status%s\n' "$C_DIM" "$C_RESET"
    elif [ -n "$ip" ] && [ "$ip" != "SERVER_IP" ] && [ "$url" != "http://${ip}:${port}" ]; then
        printf '     %s(آدرس موقت بدون دامنه: http://%s:%s/panel/login)%s\n' "$C_DIM" "$ip" "$port" "$C_RESET"
    fi
    say ""
    printf '  %s👤 نام کاربری مالک:%s %s%s%s\n' "$C_BOLD" "$C_RESET" "$C_CYAN" "$username" "$C_RESET"
    printf '  %s🔑 رمز عبور مالک:%s   %s%s%s\n' "$C_BOLD" "$C_RESET" "$C_CYAN" "$password" "$C_RESET"
    if [ "$REUSE_ENV" = "true" ]; then
        printf '     %sمقدار بالا از فایل .env خوانده شده است و فقط در اولین نصب اعمال می‌شود.%s\n' "$C_DIM" "$C_RESET"
        printf '     %sاگر رمز را از پنل عوض کرده‌اید، این رمز دیگر معتبر نیست.%s\n' "$C_DIM" "$C_RESET"
        printf '     %sبازنشانی رمز: بخش «بازنشانی رمز مالک» در docs/DEPLOYMENT.md%s\n' "$C_DIM" "$C_RESET"
    fi
    say ""
    printf '  %s%s⚠  همین حالا این رمز را یک جای امن ذخیره کنید و بعد از اولین ورود،%s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
    printf '  %s%s   از داخل پنل (حساب کاربری → تغییر رمز) رمز را عوض کنید.%s\n' "$C_BOLD" "$C_YELLOW" "$C_RESET"
    say ""
    printf '%s%s%s\n' "$C_GREEN" "$line" "$C_RESET"
    say ""
    printf '  %s🧰 دستورهای پرکاربرد (داخل پوشه‌ی %s):%s\n' "$C_BOLD" "$INSTALL_DIR" "$C_RESET"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '     %s%s--profile tls logs -f bot%s      %s→ دیدن لاگ زنده‌ی ربات%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s--profile tls logs -f caddy%s    %s→ دیدن لاگ گواهی SSL%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s restart bot%s      %s→ راه‌اندازی دوباره‌ی ربات%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s down%s             %s→ خاموش کردن سرویس‌ها (داده‌ها می‌مانند)%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
    else
        printf '     %s%s logs -f bot%s      %s→ دیدن لاگ زنده‌ی ربات%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s restart bot%s      %s→ راه‌اندازی دوباره‌ی ربات%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
        printf '     %s%s down%s             %s→ خاموش کردن سرویس‌ها (داده‌ها می‌مانند)%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
    fi
    printf '     %s%s ps%s               %s→ وضعیت سرویس‌ها%s\n' "$C_CYAN" "$COMPOSE" "$C_RESET" "$C_DIM" "$C_RESET"
    say ""
    printf '  %s💾 پشتیبان‌گیری خودکار فعال است (هر %s ساعت) و فایل‌ها در %s ذخیره می‌شوند.%s\n' \
        "$C_BOLD" "${BACKUP_HOURS_SHOWN:-24}" "$(printf '%s/backups' "$INSTALL_DIR")" "$C_RESET"
    printf '  %s🔄 برای به‌روزرسانی در آینده: %s%s\n' "$C_BOLD" "bash update.sh" "$C_RESET"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '  %s🌐 برای تغییر یا حذف دامنه: bash scripts/set-domain.sh --status%s\n' "$C_BOLD" "$C_RESET"
    else
        printf '  %s🔒 برای فعال‌کردن دامنه و SSL خودکار: bash scripts/set-domain.sh example.com%s\n' "$C_BOLD" "$C_RESET"
    fi
    say ""
    if [ "$ALLOW_UFW_HINT" = "true" ]; then
        printf '  %s🔥 اگر فایروال (ufw) فعال دارید، پورت‌ها را باز کنید:%s\n' "$C_BOLD" "$C_RESET"
        if [ "$TLS_ENABLED" = "true" ]; then
            printf '     %sufw allow 80/tcp && ufw allow 443/tcp%s\n' "$C_CYAN" "$C_RESET"
        else
            printf '     %sufw allow %s/tcp%s\n' "$C_CYAN" "$port" "$C_RESET"
        fi
        say ""
    fi
    printf '  %s🗑  برای حذف کامل: bash uninstall.sh   (داده‌ها پیش‌فرض حفظ می‌شوند)%s\n' "$C_DIM" "$C_RESET"
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
#  برنامه‌ی اصلی / main
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
        err "پلاگین Docker Compose پیدا نشد."
        say "  نصب: apt-get update && apt-get install -y docker-compose-plugin"
        say "  سپس دوباره همین اسکریپت را اجرا کنید."
        exit 1
    fi
    ok "داکر آماده است (${COMPOSE})."

    prepare_dir
    load_existing

    REUSE_ENV="false"
    ENV_BACKUP=""
    handle_existing_env

    detect_public_ip
    if [ "$PUBLIC_IP" != "SERVER_IP" ]; then
        dim "  IP سرور: ${PUBLIC_IP}"
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

    # -- حالت دامنه / بدون دامنه -------------------------------------------
    if [ -n "$DOMAIN_FINAL" ]; then
        TLS_ENABLED="true"
        COMPOSE_PROFILE="$COMPOSE --profile tls"
        PANEL_BIND_FINAL="$TLS_BIND"
    else
        TLS_ENABLED="false"
        COMPOSE_PROFILE="$COMPOSE"
        [ -n "$PANEL_BIND_FINAL" ] || PANEL_BIND_FINAL="$PANEL_BIND_DEFAULT"
    fi

    # خلاصه‌ی تنظیمات قبل از اجرا
    say ""
    printf '%s── خلاصه‌ی تنظیمات ────────────────────────────────%s\n' "$C_BOLD" "$C_RESET"
    printf '  پوشه‌ی نصب    : %s\n' "$INSTALL_DIR"
    if [ "$TLS_ENABLED" = "true" ]; then
        printf '  دامنه         : %s%s%s (SSL خودکار)\n' "$C_CYAN" "$DOMAIN_FINAL" "$C_RESET"
        printf '  ایمیل ACME    : %s\n' "${ACME_EMAIL_FINAL:-(ثبت نشده — هشدار انقضا نمی‌آید)}"
        printf '  حالت ربات     : webhook\n'
    else
        printf '  دامنه         : (بدون دامنه — بدون SSL)\n'
        printf '  حالت ربات     : polling\n'
    fi
    printf '  پورت پنل      : %s\n' "$PANEL_PORT_FINAL"
    printf '  آدرس عمومی    : %s\n' "$PANEL_URL_FINAL"
    printf '  مالک پنل      : %s\n' "$OWNER_USERNAME_SHOWN"
    printf '  مدیران        : %s\n' "$ADMIN_IDS_FINAL"
    if [ -n "${BOT_TOKEN_FINAL:-}" ]; then
        printf '  توکن ربات     : %s…%s (مخفی)\n' "$(printf '%s' "$BOT_TOKEN_FINAL" | cut -c1-10)" "$(printf '%s' "$BOT_TOKEN_FINAL" | rev | cut -c1-4 | rev)"
    fi
    printf '%s───────────────────────────────────────────────────%s\n' "$C_BOLD" "$C_RESET"

    # -- بررسی DNS قبل از درخواست گواهی ------------------------------------
    if [ "$TLS_ENABLED" = "true" ]; then
        step "بررسی اینکه دامنه به این سرور اشاره می‌کند"
        if check_dns_points_here "$DOMAIN_FINAL"; then
            ok "دامنه درست به این سرور اشاره می‌کند (${PUBLIC_IP})."
        else
            show_dns_warning
            if [ "$ASSUME_YES" = "true" ]; then
                warn "حالت --yes: بدون تأیید ادامه می‌دهیم؛ اگر DNS درست نشده باشد گواهی صادر نمی‌شود."
            elif ! confirm "با این وضعیت ادامه می‌دهیم؟ (اگر DNS هنوز درست نشده، بهتر است اول درستش کنید)" "n"; then
                say ""
                info "نصب متوقف شد. بعد از درست‌کردن رکورد DNS دوباره اجرا کنید:"
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
