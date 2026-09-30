#!/usr/bin/env bash
# ---------------------------------------------------------------------------
#  menu.sh harness — meet the control menu the way an operator does.
#
#  Every case below is a way the menu is really started:
#
#    * from inside a checkout;
#    * with the file somewhere else, while the project sits in the installer's
#      default location (what the README's `curl` one-liner gives you);
#    * through a pipe, where the script has no path of its own;
#    * as an ordinary user, where sudo has to re-run it;
#    * on a fresh server, where nothing is installed yet.
#
#  Docker is stubbed, curl is stubbed, root is stubbed: no daemon, no network,
#  nothing outside a temporary directory.  Every case asserts its outcome, so a
#  change that breaks discovery, the sudo re-exec or the child-script fallback
#  fails here instead of on a live server.
#
#  Run:  bash tools/menu_harness.sh          (Linux; WSL on Windows)
#        KEEP=1 bash tools/menu_harness.sh   (leave the scratch tree behind)
# ---------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MENU="${MENU_UNDER_TEST:-$HERE/../menu.sh}"
BASE_PATH="$PATH"

if [ ! -f "$MENU" ]; then
    printf 'menu.sh not found at %s\n' "$MENU" >&2
    exit 2
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/wggb-menu-harness-XXXXXX")"
[ "${KEEP:-0}" = "1" ] || trap 'rm -rf "$WORK"' EXIT

PROJ="$WORK/proj"            # a directory that looks installed
HOME_DIR="$WORK/home"        # $HOME for the installed cases: holds wg-guard-bot/
DEFAULT_PROJ="$HOME_DIR/wg-guard-bot"
EMPTY_HOME="$WORK/empty-home"  # a server where nothing is installed
MIRROR="$WORK/released"      # stands in for raw.githubusercontent.com
OUT="$WORK/out"
STUB="$WORK/stub"            # root, docker, curl
STUB_USER="$WORK/stub-user"  # a normal user with a sudo that only records argv

mkdir -p "$PROJ" "$DEFAULT_PROJ" "$MIRROR" "$OUT" "$STUB" "$STUB_USER" "$EMPTY_HOME" "$HOME_DIR/sub"
cp "$MENU" "$MIRROR/menu.sh"

# --- stubs -----------------------------------------------------------------
cat > "$STUB/id" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ]; then echo 0; exit 0; fi
exec /usr/bin/id "$@"
EOF

cat > "$STUB_USER/id" <<'EOF'
#!/usr/bin/env bash
if [ "${1:-}" = "-u" ]; then echo 1000; exit 0; fi
exec /usr/bin/id "$@"
EOF

cat > "$STUB_USER/sudo" <<'EOF'
#!/usr/bin/env bash
echo "SUDO-ARGV $*"
EOF

# `docker` answers the few questions the menu asks, without a daemon.
cat > "$STUB/docker" <<'EOF'
#!/usr/bin/env bash
case "${1:-}" in
    compose)
        shift
        case "${1:-}" in
            version) echo "Docker Compose version v2.29.0" ;;
            *) exit 0 ;;
        esac
        ;;
    *) exit 0 ;;
esac
EOF

# `curl` serves the release mirror, and fails like `curl -f` for anything else.
cat > "$STUB/curl" <<'EOF'
#!/usr/bin/env bash
url=""; dest=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o) dest="${2:-}"; shift ;;
        -*) ;;
        *) url="$1" ;;
    esac
    shift
done
src="${WGG_HARNESS_MIRROR:-}/${url##*/}"
if [ -z "$dest" ] || [ ! -f "$src" ]; then
    echo "curl: (22) stub: nothing mirrored for $url" >&2
    exit 22
fi
cp "$src" "$dest"
EOF

chmod +x "$STUB/id" "$STUB/docker" "$STUB/curl" "$STUB_USER/id" "$STUB_USER/sudo"
# The normal-user PATH gets the same docker/curl stubs: a case that quietly fell
# through to the real curl would "pass" by downloading the real menu.sh.
cp "$STUB/docker" "$STUB/curl" "$STUB_USER/"

# --- the project these cases manage ----------------------------------------
printf 'services: {}\n' > "$PROJ/docker-compose.yml"
printf 'PANEL_PORT=8080\nPOSTGRES_USER=wgguard\nPOSTGRES_DB=wgguard\n' > "$PROJ/.env"
cp "$PROJ/docker-compose.yml" "$PROJ/.env" "$DEFAULT_PROJ/"

cat > "$DEFAULT_PROJ/update.sh" <<'EOF'
#!/usr/bin/env bash
echo "CHILD default update.sh ran in $(pwd)"
EOF
cat > "$DEFAULT_PROJ/install.sh" <<'EOF'
#!/usr/bin/env bash
echo "CHILD released install.sh ran in $(pwd) with: $*"
EOF
chmod +x "$DEFAULT_PROJ/update.sh" "$DEFAULT_PROJ/install.sh"
cp "$DEFAULT_PROJ/install.sh" "$MIRROR/install.sh"
cat > "$PROJ/update.sh" <<'EOF'
#!/usr/bin/env bash
echo "CHILD update.sh ran in $(pwd)"
EOF
chmod +x "$PROJ/update.sh"

# --- assertions ------------------------------------------------------------
PASS=0
FAIL=0
ok()  { printf '  ok    %s\n' "$1"; PASS=$(( PASS + 1 )); }
bad() { printf '  FAIL  %s\n' "$1"; sed 's/^/        | /' "$2" | tail -n 6; FAIL=$(( FAIL + 1 )); }

expect_in() {
    # expect_in <label> <file> <extended regex>
    if grep -aqE "$3" "$2"; then ok "$1"; else bad "$1" "$2"; fi
}

expect_not_in() {
    if grep -aqE "$3" "$2"; then bad "$1" "$2"; else ok "$1"; fi
}

section() { printf '\n%s\n' "$1"; }

# --- how a case is started --------------------------------------------------
#: Set by as_root / as_normal_user; read by run_menu.  Explicit, because a
#: `VAR=x func` prefix does not reliably reach the processes a function starts.
RUN_STUB="$STUB"
RUN_HOME="$HOME_DIR"

as_root()   { RUN_STUB="$STUB"; RUN_HOME="${1:-$HOME_DIR}"; }
as_normal() { RUN_STUB="$STUB_USER"; RUN_HOME="${1:-$HOME_DIR}"; }

run_menu() {
    # run_menu <out file> <cwd> <path mode> <release mirror> [args…]
    #   path mode: file  — bash /path/to/menu.sh
    #              pipe  — bash <(cat menu.sh)   (what the README one-liner does)
    local out="$1" cwd="$2" mode="$3" mirror="$4"
    shift 4
    (
        cd "$cwd" || exit 1
        export PATH="$RUN_STUB:$BASE_PATH"
        export HOME="$RUN_HOME"
        export WGG_HARNESS_MIRROR="$mirror"
        if [ "$mode" = "pipe" ]; then
            bash <(cat "$MIRROR/menu.sh") "$@"
        else
            bash "$MIRROR/menu.sh" "$@"
        fi
    ) > "$out" 2>&1
    return $?
}

# ===========================================================================
#  1) the ordinary case: a checkout, started from inside it
# ===========================================================================
section "1) checkout beside the menu, cwd = the project"
cp "$MENU" "$PROJ/menu.sh"
(
    cd "$PROJ" || exit 1
    PATH="$STUB:$BASE_PATH" HOME="$HOME_DIR" bash ./menu.sh update
) > "$OUT/01.txt" 2>&1
expect_in "runs the project's own update.sh" "$OUT/01.txt" "CHILD update.sh ran in $PROJ"

# ===========================================================================
#  2) the regression: the menu lives elsewhere, the project in the default dir
# ===========================================================================
section "2) menu.sh in another directory, project in \$HOME/wg-guard-bot"
as_root
run_menu "$OUT/02.txt" "$HOME_DIR" file "$MIRROR" update
expect_in "finds the installed project" "$OUT/02.txt" "CHILD default update.sh ran in $DEFAULT_PROJ"
expect_not_in "no bogus 'not installed yet'" "$OUT/02.txt" "not installed yet"

# ===========================================================================
#  3) the README one-liner, from an unrelated directory
# ===========================================================================
section "3) bash <(curl …/menu.sh) from an unrelated directory"
as_root
run_menu "$OUT/03.txt" "$HOME_DIR/sub" pipe "$MIRROR" update
expect_in "still finds the installed project" "$OUT/03.txt" "CHILD default update.sh ran in $DEFAULT_PROJ"

# ===========================================================================
#  4) --dir always wins
# ===========================================================================
section "4) --dir overrides discovery"
as_root
run_menu "$OUT/04.txt" / file "$MIRROR" --dir "$PROJ" update
expect_in "--dir is honoured" "$OUT/04.txt" "CHILD update.sh ran in $PROJ"

# ===========================================================================
#  5) a child that fails comes back to the menu
# ===========================================================================
section "5) a failing child script"
cat > "$DEFAULT_PROJ/update.sh" <<'EOF'
#!/usr/bin/env bash
echo "CHILD update.sh is failing on purpose"
exit 3
EOF
chmod +x "$DEFAULT_PROJ/update.sh"

as_root
run_menu "$OUT/05.txt" "$HOME_DIR" file "$MIRROR" update
if [ "$?" -eq 3 ]; then ok "one command exits with the child's status"; else bad "one command exit status" "$OUT/05.txt"; fi
expect_in "the child's own output is shown" "$OUT/05.txt" "failing on purpose"

if command -v script >/dev/null 2>&1; then
    ( cd "$HOME_DIR" && printf '2\n\n0\n' | PATH="$STUB:$BASE_PATH" HOME="$HOME_DIR" \
        WGG_HARNESS_MIRROR="$MIRROR" script -qec "bash $MIRROR/menu.sh --ascii" /dev/null ) > "$OUT/05b.txt" 2>&1
    expect_in "the interactive menu reports the failure" "$OUT/05b.txt" "The update did not finish"
    expect_in "and returns to the menu" "$OUT/05b.txt" "Press Enter to return to the menu"
else
    printf '  skip  interactive checks (no `script` for a pty)\n'
fi

cat > "$DEFAULT_PROJ/update.sh" <<'EOF'
#!/usr/bin/env bash
echo "CHILD default update.sh ran in $(pwd)"
EOF
chmod +x "$DEFAULT_PROJ/update.sh"

# ===========================================================================
#  6) an ordinary user: sudo must be handed something it can read
# ===========================================================================
section "6) not root"
as_normal
run_menu "$OUT/06.txt" "$HOME_DIR" file "$MIRROR" status
expect_in "sudo gets the real menu path" "$OUT/06.txt" "SUDO-ARGV -E bash $MIRROR/menu.sh"

as_normal
run_menu "$OUT/07.txt" "$HOME_DIR" pipe "$MIRROR" status
expect_in "sudo gets a readable copy when we arrived through a pipe" "$OUT/07.txt" "SUDO-ARGV -E bash /.*wgguard-menu-.*\.sh"

# ===========================================================================
#  7) nothing installed and no way to download: refuse, and say what to do
# ===========================================================================
section "7) not root, nothing installed, download impossible"
if [ -e /root/wg-guard-bot ]; then
    printf '  skip  /root/wg-guard-bot exists on this machine\n'
else
    as_normal "$EMPTY_HOME"
    run_menu "$OUT/08.txt" /tmp pipe "$WORK/nope" status
    expect_in "the refusal is printed" "$OUT/08.txt" "no path to re-run"
    expect_in "with a way forward" "$OUT/08.txt" "curl -fsSL"
    expect_not_in "nothing was re-run" "$OUT/08.txt" "SUDO-ARGV"
fi

# ===========================================================================
#  8) a fresh server: the menu must still be able to install
# ===========================================================================
section "8) fresh server, nothing installed, item 1"
as_root "$EMPTY_HOME"
run_menu "$OUT/09.txt" "$EMPTY_HOME" pipe "$MIRROR" install
expect_in "the released install.sh is fetched and run" "$OUT/09.txt" "CHILD released install\.sh"
expect_in "pointed at the directory the menu manages" "$OUT/09.txt" "with: --dir $EMPTY_HOME/wg-guard-bot"
expect_not_in "no 'was not found in /dev/fd'" "$OUT/09.txt" "was not found in /dev/fd"

# ===========================================================================
#  Result
# ===========================================================================
printf '\n%d passed, %d failed\n' "$PASS" "$FAIL"
if [ "$FAIL" -gt 0 ]; then
    printf 'scratch tree kept at %s\n' "$WORK"
    trap - EXIT
    exit 1
fi
exit 0
