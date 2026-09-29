#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — به‌روزرسانی / updater
#
#  کارهایی که این اسکریپت انجام می‌دهد:
#    ۱) یک پشتیبان سریع از دیتابیس می‌گیرد (اگر سرویس‌ها بالا باشند)
#    ۲) آخرین کد را می‌گیرد (git pull --ff-only) یا در صورت نبود git، فقط
#       ایمیج را از نو می‌سازد
#    ۳) ایمیج را دوباره build و سرویس‌ها را با کد جدید بالا می‌آورد
#    ۴) مایگریشن‌های Alembic را داخل کانتینر اجرا می‌کند
#    ۵) نسخه‌ی نهایی را چاپ می‌کند
#
#  اجرا:  bash update.sh [--branch main] [--dir PATH] [--no-backup] [--yes]
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
${C_BOLD}WG-Guard Bot — به‌روزرسانی${C_RESET}

${C_BOLD}استفاده:${C_RESET}
  bash update.sh [گزینه‌ها]

${C_BOLD}گزینه‌ها:${C_RESET}
  ${C_CYAN}--branch NAME${C_RESET}   شاخه‌ای که به‌روزرسانی از آن گرفته می‌شود (پیش‌فرض: main)
  ${C_CYAN}--dir PATH${C_RESET}      مسیر پوشه‌ی پروژه (پیش‌فرض: پوشه‌ی فعلی)
  ${C_CYAN}--no-backup${C_RESET}     قبل از به‌روزرسانی پشتیبان نگیر
  ${C_CYAN}--yes${C_RESET}           بدون پرسش، همه‌چیز را تأیید کن
  ${C_CYAN}-h, --help${C_RESET}      نمایش همین راهنما

${C_DIM}English: pulls the latest code (fast-forward only), rebuilds the image,
restarts the stack, runs 'alembic upgrade head' inside the container and prints
the resulting version.${C_RESET}
EOF
}

require_root() {
    if [ "$(id -u)" -eq 0 ]; then
        return 0
    fi
    if command -v sudo >/dev/null 2>&1; then
        warn "این اسکریپت به دسترسی root نیاز دارد؛ با sudo دوباره اجرا می‌شود…"
        exec sudo -E bash "$0" "$@"
    fi
    err "برای به‌روزرسانی به دسترسی root نیاز است (و sudo نصب نیست)."
    exit 1
}

detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE="docker compose"
        return 0
    fi
    # Compose v1 is end-of-life and lacks --profile / ps --status.
    if command -v docker-compose >/dev/null 2>&1; then
        warn "نسخه‌ی قدیمی docker-compose (v1) نصب است؛ این اسکریپت به Docker Compose v2 نیاز دارد."
        say "  نصب: apt-get update && apt-get install -y docker-compose-plugin"
    fi
    err "Docker Compose پیدا نشد. نصب: apt-get install -y docker-compose-plugin"
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
            *) err "گزینه‌ی ناشناخته: $1"; say ""; usage; exit 2 ;;
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
#  ۱) پشتیبان پیش از به‌روزرسانی
# ---------------------------------------------------------------------------
take_backup() {
    if [ "$DO_BACKUP" != "true" ]; then
        warn "پشتیبان‌گیری نادیده گرفته شد (--no-backup)."
        return 0
    fi
    step "پشتیبان‌گیری پیش از به‌روزرسانی"
    if ! running_services | grep -q '^db$'; then
        warn "دیتابیس در حال اجرا نیست؛ از این مرحله رد می‌شویم."
        return 0
    fi
    local stamp
    stamp="$(date +%Y%m%d-%H%M%S)"
    if $COMPOSE exec -T db sh -c \
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' > "./pre-update-${stamp}.sql" 2>/dev/null; then
        if [ -s "./pre-update-${stamp}.sql" ]; then
            ok "پشتیبان ذخیره شد: ./pre-update-${stamp}.sql"
            return 0
        fi
        rm -f "./pre-update-${stamp}.sql"
        warn "پشتیبان خالی بود و پاک شد."
        return 0
    fi
    rm -f "./pre-update-${stamp}.sql"
    warn "پشتیبان‌گیری ناموفق بود؛ ادامه می‌دهیم (پشتیبان‌های خودکار ربات سر جایشان هستند)."
    return 0
}

# ---------------------------------------------------------------------------
#  ۲) گرفتن آخرین کد
# ---------------------------------------------------------------------------
current_version() {
    # اول از git، بعد از pyproject.toml، در نهایت «نامشخص»
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
    printf 'نامشخص'
    return 0
}

pull_code() {
    step "دریافت آخرین تغییرات کد"
    if [ ! -d "$INSTALL_DIR/.git" ]; then
        warn "این پوشه یک مخزن git نیست؛ از دریافت کد رد می‌شویم و فقط ایمیج را از نو می‌سازیم."
        dim "  (برای به‌روزرسانی خودکار کد، پروژه را با git clone نصب کنید)"
        return 0
    fi
    if ! command -v git >/dev/null 2>&1; then
        warn "git نصب نیست؛ از دریافت کد رد می‌شویم."
        return 0
    fi
    if ! git -C "$INSTALL_DIR" rev-parse --git-dir >/dev/null 2>&1; then
        warn "پوشه‌ی .git وجود دارد اما یک مخزن سالم نیست؛ از دریافت کد رد می‌شویم."
        return 0
    fi

    local dirty
    dirty="$(git -C "$INSTALL_DIR" status --porcelain 2>/dev/null | grep -v '^?? \.env' || true)"
    if [ -n "$dirty" ]; then
        warn "تغییرات محلی در کد وجود دارد:"
        printf '%s\n' "$dirty" | head -n 10 | sed -e 's/^/    /' >&2
        if [ "$ASSUME_YES" != "true" ]; then
            if confirm "تغییرات محلی نادیده گرفته شوند (stash) و به‌روزرسانی ادامه یابد؟" "y"; then
                git -C "$INSTALL_DIR" stash push -u -m "update.sh $(date +%Y%m%d-%H%M%S)" >/dev/null 2>&1 || true
                ok "تغییرات موقتاً کنار گذاشته شدند (git stash)."
            else
                err "به‌روزرسانی لغو شد؛ اول تغییرات محلی را commit یا stash کنید."
                exit 1
            fi
        else
            warn "حالت --yes: تغییرات محلی نادیده گرفته و stash می‌شوند."
            git -C "$INSTALL_DIR" stash push -u -m "update.sh $(date +%Y%m%d-%H%M%S)" >/dev/null 2>&1 || true
        fi
    fi

    if git -C "$INSTALL_DIR" pull --ff-only origin "$BRANCH"; then
        ok "کد به آخرین نسخه‌ی شاخه‌ی ${BRANCH} به‌روز شد."
    else
        err "git pull --ff-only ناموفق بود (شاید شاخه‌ی محلی از ریموت جلوتر است)."
        say "  راه‌حل دستی:"
        dim "    cd $INSTALL_DIR && git fetch origin && git reset --hard origin/$BRANCH"
        say "  سپس همین اسکریپت را دوباره اجرا کنید."
        exit 1
    fi
    return 0
}

# ---------------------------------------------------------------------------
#  ۳) ساخت و اجرای دوباره
# ---------------------------------------------------------------------------
rebuild_and_restart() {
    step "ساخت ایمیج جدید و راه‌اندازی دوباره"
    $COMPOSE pull --ignore-pull-failures 2>/dev/null || true
    if ! $COMPOSE build --pull bot; then
        err "ساخت ایمیج ناموفق بود."
        return 1
    fi
    ok "ایمیج جدید ساخته شد."
    if ! $COMPOSE up -d --remove-orphans; then
        err "اجرای سرویس‌ها ناموفق بود."
        return 1
    fi
    ok "سرویس‌ها با نسخه‌ی جدید اجرا شدند."
    return 0
}

# ---------------------------------------------------------------------------
#  ۴) مایگریشن‌ها
# ---------------------------------------------------------------------------
run_migrations() {
    step "اجرای مایگریشن‌های دیتابیس"
    local tries=1
    while [ "$tries" -le 20 ]; do
        if $COMPOSE exec -T bot alembic upgrade head; then
            ok "دیتابیس به آخرین نسخه‌ی مایگریشن رسید."
            return 0
        fi
        dim "  تلاش ${tries}/20 — ربات هنوز آماده نیست، ۳ ثانیه صبر…"
        sleep 3
        tries=$(( tries + 1 ))
    done
    err "اجرای مایگریشن ناموفق بود."
    dim "  لاگ ربات:  $COMPOSE logs --tail=40 bot"
    return 1
}

# ---------------------------------------------------------------------------
#  ۵) بررسی سلامت و چاپ نسخه
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
    step "بررسی سلامت سرویس (تا ${HEALTH_TIMEOUT} ثانیه)"
    while [ "$waited" -lt "$HEALTH_TIMEOUT" ]; do
        if command -v curl >/dev/null 2>&1 && curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
            ok "پنل سالم است: $url"
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
    warn "پنل در ${HEALTH_TIMEOUT} ثانیه پاسخ نداد؛ لاگ‌ها را بررسی کنید:"
    say "    $COMPOSE logs --tail=40 bot"
    return 1
}

app_version() {
    # نسخه را از داخل خود کانتینر می‌پرسیم؛ اگر نشد، از کد محلی
    local ver=""
    ver="$($COMPOSE exec -T bot python -c \
        'import importlib.metadata as m; print(m.version("wg-guard-bot"))' 2>/dev/null | tr -d '\r\n' || true)"
    if [ -z "$ver" ]; then
        ver="$(current_version)"
    fi
    printf '%s' "$ver"
    return 0
}

# ===========================================================================
#  main
# ===========================================================================
main() {
    parse_args "$@"

    # اول بررسی root؛ تا در حالت نیاز به sudo خروجی تکراری چاپ نشود
    require_root "$@"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — به‌روزرسانی${C_RESET}"

    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
        err "فایل docker-compose.yml در «$INSTALL_DIR» پیدا نشد."
        say "  با --dir مسیر درست را بدهید، مثلاً: bash update.sh --dir /root/wg-guard-bot"
        exit 1
    fi
    cd "$INSTALL_DIR"
    if [ ! -f .env ]; then
        err "فایل .env پیدا نشد؛ به‌نظر می‌رسد هنوز نصب نکرده‌اید."
        say "  اول نصب کنید: bash install.sh"
        exit 1
    fi

    detect_compose
    ok "داکر آماده است (${COMPOSE})."

    VERSION_BEFORE="$(current_version)"
    dim "  نسخه‌ی فعلی: ${VERSION_BEFORE}"

    take_backup
    pull_code

    if ! rebuild_and_restart; then
        say ""
        err "به‌روزرسانی ناتمام ماند."
        $COMPOSE ps 2>&1 | tail -n 15 || true
        say ""
        $COMPOSE logs --tail=40 bot 2>&1 || true
        say ""
        dim "برای بازگشت، نسخه‌ی قبلی ایمیج را با تگ مشخص در docker-compose.override.yml اجرا کنید."
        exit 1
    fi

    if ! run_migrations; then
        $COMPOSE logs --tail=40 bot 2>&1 || true
        exit 1
    fi

    wait_health "$(panel_port)" || true

    VERSION_AFTER="$(app_version)"
    [ -n "$VERSION_AFTER" ] || VERSION_AFTER="نامشخص"

    say ""
    printf '%s%s%s\n' "$C_GREEN" "════════════════════════════════════════════════════" "$C_RESET"
    printf '%s  ✅  به‌روزرسانی با موفقیت انجام شد.%s\n' "$C_GREEN" "$C_RESET"
    printf '%s%s%s\n' "$C_GREEN" "════════════════════════════════════════════════════" "$C_RESET"
    say ""
    printf '  نسخه‌ی قبلی : %s\n' "$VERSION_BEFORE"
    printf '  نسخه‌ی جدید : %s%s%s\n' "$C_BOLD" "$VERSION_AFTER" "$C_RESET"
    printf '  شاخه       : %s\n' "$BRANCH"
    say ""
    info "دستورهای مفید:"
    printf '     %s logs -f bot%s      → دیدن لاگ زنده\n' "$COMPOSE" "$C_RESET"
    printf '     %s ps%s               → وضعیت سرویس‌ها\n' "$COMPOSE" "$C_RESET"
    say ""
    return 0
}

main "$@"
