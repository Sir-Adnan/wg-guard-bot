"""Middleware package.

Execution order for an update coming from a private chat::

    DatabaseMiddleware   -> one AsyncSession per update, committed on success
    ContextMiddleware    -> User + Staff records, maintenance/ban gate
    MembershipMiddleware -> forced channel join gate
    ThrottleMiddleware   -> per-user rate limit
"""

from __future__ import annotations

from app.bot.middlewares.context import ContextMiddleware, DatabaseMiddleware
from app.bot.middlewares.membership import MembershipMiddleware
from app.bot.middlewares.throttle import ThrottleMiddleware

__all__ = [
    "ContextMiddleware",
    "DatabaseMiddleware",
    "MembershipMiddleware",
    "ThrottleMiddleware",
]
