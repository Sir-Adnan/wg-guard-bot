#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  WG-Guard Bot — حذف / uninstaller
#
#  پیش‌فرض: فقط کانتینرها و شبکه‌ی سرویس حذف می‌شوند.
#           «داده‌های دیتابیس، Redis و پشتیبان‌ها دست‌نخورده می‌مانند.»
#
#  با --purge: حجم‌های داده (pgdata، redisdata، backups) و پوشه‌ی backups/
#              هم حذف می‌شوند — فقط بعد از تأیید تایپی «yes».
#
#  اجرا:  bash uninstall.sh [--purge] [--dir PATH] [--yes] [--keep-images]
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
${C_BOLD}WG-Guard Bot — حذف${C_RESET}

${C_BOLD}استفاده:${C_RESET}
  bash uninstall.sh [گزینه‌ها]

${C_BOLD}گزینه‌ها:${C_RESET}
  ${C_CYAN}--purge${C_RESET}         حذف کامل داده‌ها (دیتابیس، Redis و پشتیبان‌ها) — با تأیید تایپی
  ${C_CYAN}--keep-images${C_RESET}   ایمیج ساخته‌شده را نگه دار
  ${C_CYAN}--dir PATH${C_RESET}      مسیر پوشه‌ی پروژه (پیش‌فرض: پوشه‌ی فعلی)
  ${C_CYAN}--yes${C_RESET}           بدون پرسش‌های تأیید (تأیید تایپی --purge حذف نمی‌شود)
  ${C_CYAN}-h, --help${C_RESET}      نمایش همین راهنما

${C_BOLD}تفاوت دو حالت:${C_RESET}
  بدون --purge : کانتینرها می‌روند، ${C_BOLD}داده‌ها می‌مانند${C_RESET} (نصب بعدی همان داده‌ها را برمی‌گرداند)
  با --purge   : همه‌چیز می‌رود؛ ${C_BOLD}دیگر راه بازگشتی نیست${C_RESET}

${C_DIM}English: stops and removes the containers.  Volumes and data are KEPT by
default; --purge deletes them after an explicit typed confirmation.${C_RESET}
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
    err "برای حذف سرویس‌ها به دسترسی root نیاز است (و sudo نصب نیست)."
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

# ===========================================================================
#  main
# ===========================================================================
main() {
    parse_args "$@"

    # اول بررسی root؛ تا در حالت نیاز به sudo خروجی تکراری چاپ نشود
    require_root "$@"

    printf '%s\n' "${C_BOLD}WG-Guard Bot — حذف${C_RESET}"

    if [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
        err "فایل docker-compose.yml در «$INSTALL_DIR» پیدا نشد."
        say "  با --dir مسیر درست را بدهید، مثلاً: bash uninstall.sh --dir /root/wg-guard-bot"
        exit 1
    fi
    cd "$INSTALL_DIR"

    detect_compose
    if [ -z "$COMPOSE" ]; then
        err "Docker Compose پیدا نشد؛ نمی‌توان سرویس‌ها را مدیریت کرد."
        say "  اگر داکر را دستی حذف کرده‌اید، کانتینرها هم از قبل نیستند."
        exit 1
    fi

    # -----------------------------------------------------------------------
    #  حالت پیش‌فرض: فقط کانتینرها، داده‌ها حفظ می‌شوند
    # -----------------------------------------------------------------------
    if [ "$PURGE" != "true" ]; then
        say ""
        info "حالت پیش‌فرض: کانتینرها حذف می‌شوند اما داده‌ها باقی می‌مانند. 💾"
        dim "  یعنی دیتابیس، Redis و پشتیبان‌ها دست‌نخورده‌اند و با اجرای دوباره‌ی"
        dim "  install.sh همان اطلاعات برمی‌گردد."
        say ""

        if ! confirm "سرویس‌ها متوقف و کانتینرها حذف شوند؟" "y"; then
            info "لغو شد؛ هیچ تغییری اعمال نشد."
            exit 0
        fi

        step "توقف سرویس‌ها"
        $COMPOSE down --remove-orphans || true
        ok "کانتینرها و شبکه حذف شدند."

        if [ "$KEEP_IMAGES" != "true" ]; then
            step "حذف ایمیج ساخته‌شده‌ی ربات (کش build دست‌نخورده می‌ماند)"
            docker image rm -f "${IMAGE_NAME:-wgguard-bot}:${IMAGE_TAG:-latest}" >/dev/null 2>&1 || true
            ok "ایمیج حذف شد."
        fi

        say ""
        printf '%s%s%s\n' "$C_YELLOW" "════════════════════════════════════════════════════" "$C_RESET"
        printf '%s  💾  داده‌های شما حفظ شد — هیچ اطلاعاتی پاک نشد.%s\n' "$C_YELLOW" "$C_RESET"
        printf '%s%s%s\n' "$C_YELLOW" "════════════════════════════════════════════════════" "$C_RESET"
        say ""
        info "این‌ها هنوز روی سرور هستند:"
        printf '   • دیتابیس پستگرس  : volume «%spgdata%s»\n' "$C_CYAN" "$C_RESET"
        printf '   • داده‌های Redis   : volume «%sredisdata%s»\n' "$C_CYAN" "$C_RESET"
        printf '   • پشتیبان‌ها        : volume «%sbackups%s»\n' "$C_CYAN" "$C_RESET"
        printf '   • فایل تنظیمات     : %s (شامل رمزها)\n' "${INSTALL_DIR}/.env"
        say ""
        info "برای برگرداندن سرویس‌ها:"
        dim "    cd $INSTALL_DIR && $COMPOSE up -d"
        say ""
        warn "اگر واقعاً می‌خواهید همه‌چیز پاک شود: bash uninstall.sh --purge"
        say ""
        return 0
    fi

    # -----------------------------------------------------------------------
    #  حالت --purge: حذف کامل با تأیید تایپی
    # -----------------------------------------------------------------------
    say ""
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    printf '%s%s  🚨  هشدار: حالت حذف کامل (--purge) فعال است!%s\n' "$C_BOLD" "$C_RED" "$C_RESET"
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    say ""
    say "  با ادامه، این‌ها ${C_BOLD}برای همیشه${C_RESET} پاک می‌شوند:"
    printf '   %s✖%s همه‌ی کاربران، سفارش‌ها، رسیدها و تنظیمات دیتابیس\n' "$C_RED" "$C_RESET"
    printf '   %s✖%s وضعیت گفتگوها و کش Redis\n' "$C_RED" "$C_RESET"
    printf '   %s✖%s همه‌ی فایل‌های پشتیبان (wgguard-*.sql)\n' "$C_RED" "$C_RESET"
    say ""
    warn "پیشنهاد: قبل از ادامه یک نسخه‌ی پشتیبان روی کامپیوتر خودتان بگیرید."
    dim "    $COMPOSE exec -T db pg_dump -U \"\$POSTGRES_USER\" \"\$POSTGRES_DB\" > backup.sql"
    say ""

    if [ "$ASSUME_YES" != "true" ]; then
        printf '%sبرای تأیید، کلمه‌ی yes را تایپ کنید (هر چیز دیگری = لغو):%s\n  %s❯%s ' \
            "$C_BOLD" "$C_RESET" "$C_RED" "$C_RESET" >&2
        typed=""
        IFS= read -r typed || typed=""
        if [ "$typed" != "yes" ]; then
            info "لغو شد؛ هیچ داده‌ای پاک نشد. ✅"
            exit 0
        fi
    else
        warn "حالت --yes: از تأیید تایپی رد می‌شویم (خودتان --purge را خواسته‌اید)."
    fi

    step "توقف سرویس‌ها و حذف کانتینرها"
    $COMPOSE down --remove-orphans || true
    ok "کانتینرها و شبکه حذف شدند."

    step "حذف حجم‌های داده"
    # اول حجم‌های نام‌دار پروژه (پیشوند نام پروژه در compose مشخص شده)
    $COMPOSE down --volumes --remove-orphans || true
    for vol in pgdata redisdata backups wgguard_pgdata wgguard_redisdata wgguard_backups; do
        if docker volume inspect "$vol" >/dev/null 2>&1; then
            docker volume rm -f "$vol" >/dev/null 2>&1 && ok "حجم «$vol» حذف شد." || warn "حذف حجم «$vol» ناموفق بود."
        fi
    done

    step "حذف پوشه‌ی پشتیبان‌ها روی دیسک"
    if [ -d "./backups" ]; then
        # فقط مسیر تأییدشده را حذف می‌کنیم
        resolved="$(cd ./backups 2>/dev/null && pwd || true)"
        if [ -n "$resolved" ] && [ "$resolved" = "$(pwd)/backups" ]; then
            rm -rf -- "$resolved"
            ok "پوشه‌ی backups حذف شد: $resolved"
        else
            warn "مسیر پوشه‌ی backups مطابق انتظار نبود؛ برای امنیت حذف نشد: ${resolved:-?}"
        fi
    else
        dim "  پوشه‌ی backups وجود نداشت."
    fi

    if [ "$KEEP_IMAGES" != "true" ]; then
        step "حذف ایمیج ربات"
        docker image rm -f "${IMAGE_NAME:-wgguard-bot}:${IMAGE_TAG:-latest}" >/dev/null 2>&1 || true
        ok "ایمیج حذف شد."
    fi

    say ""
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    printf '%s  🗑  حذف کامل انجام شد.%s\n' "$C_RED" "$C_RESET"
    printf '%s%s%s\n' "$C_RED" "════════════════════════════════════════════════════" "$C_RESET"
    say ""
    info "فایل .env روی دیسک باقی مانده است (رمزها). اگر لازم نیست، خودتان پاکش کنید:"
    dim "    rm -f $INSTALL_DIR/.env"
    say ""
    info "برای نصب دوباره از صفر:"
    dim "    cd $INSTALL_DIR && bash install.sh"
    say ""
    return 0
}

main "$@"
