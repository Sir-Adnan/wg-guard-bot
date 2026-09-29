"""Operator CLI — run it inside the container::

    docker compose exec bot python -m app.cli set-password --login admin

The panel owner account is seeded on every start (see
:mod:`app.services.bootstrap`), so the only reasons to reach for this command are
a lost password, a password that no longer matches ``OWNER_PASSWORD``, or a
second operator who needs panel access.

Output is English on purpose: a Linux console has no bidi support and renders
Persian reversed, so a Persian message here would be unreadable.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import secrets
import string
import sys
from contextlib import suppress

from app.core.config import settings
from app.core.logging import get_logger
from app.core.security import password_strength_error
from app.db.session import session_scope
from app.services.bootstrap import owner_login, set_panel_password

log = get_logger(__name__)


def _generate_password(length: int = 20) -> str:
    """A password the panel's own policy always accepts.

    A plain run of :func:`random_token` is not enough: about 3 % of twenty random
    characters contain no digit at all, and the panel rejects those.
    """
    password = [secrets.choice(string.ascii_letters), secrets.choice(string.digits)]
    pool = string.ascii_letters + string.digits
    password += [secrets.choice(pool) for _ in range(max(0, length - len(password)))]
    secrets.SystemRandom().shuffle(password)
    return "".join(password)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="WG-Guard Bot operator commands (run inside the bot container).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    set_password = commands.add_parser(
        "set-password",
        help="set or create the password of a panel login",
        description=(
            "Set the password of a panel login, creating the account when it does not exist. "
            "The account is created with the owner role."
        ),
    )
    set_password.add_argument("--login", default="", help=f"panel username (default: {owner_login()})")
    set_password.add_argument("--password", default="", help="the new password; omit to be prompted for it")
    set_password.add_argument("--generate", action="store_true", help="generate a strong password and print it")
    return parser


async def _set_password(args: argparse.Namespace) -> int:
    login = (args.login or owner_login()).strip()
    password = args.password
    if args.generate:
        password = password or _generate_password()
    if not password:
        password = getpass.getpass(f"New password for {login!r}: ")
        if password != getpass.getpass("Repeat the password: "):
            print("✖ The two passwords are not identical.", file=sys.stderr)
            return 2

    # ``password_strength_error`` is the single source of truth for the policy;
    # only its wording is Persian, so the reason is restated here.
    if password_strength_error(password):
        print("✖ Password rejected: use at least 8 characters, mixing letters and digits.", file=sys.stderr)
        return 2

    try:
        async with session_scope() as session:
            row = await set_panel_password(session, login=login, password=password)
            staff_id, role, active = row.id, row.role.value, row.is_active
    except Exception as exc:
        # The operator wants the cause, not a SQLAlchemy traceback; the full one is
        # in the log for anyone who needs it.
        print(f"✖ Could not set the password: {exc}", file=sys.stderr)
        return 1

    print(f"✔ Password set for {login!r} (staff id {staff_id}, role {role}).")
    if args.generate:
        print(f"  Generated password: {password}")
    if not active:
        print("  ⚠ This account is disabled — re-enable it from Staff in the panel.")
    print(f"  Sign in at {settings.panel_login_url}")
    return 0


async def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "set-password":
        return await _set_password(args)
    print(f"✖ Unknown command: {args.command}", file=sys.stderr)  # pragma: no cover - argparse rejects it first
    return 2


def run() -> None:
    # A console that cannot encode ✔ / ✖ (Windows cp1252, a container started with
    # LC_ALL=C) must not turn a successful password change into a traceback.
    for stream in (sys.stdout, sys.stderr):
        with suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    run()


__all__ = ["main", "run"]
