"""Operator-facing bot handlers.

Order matters: the receipt queue and the operator menu are registered before the
customer routers so that a staff callback is never swallowed by a generic menu
handler.
"""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers.admin import broadcast as admin_broadcast
from app.bot.handlers.admin import panel as admin_panel
from app.bot.handlers.admin import receipts as admin_receipts
from app.bot.handlers.admin import users as admin_users

router = Router(name="admin")
router.include_router(admin_receipts.router)
router.include_router(admin_panel.router)
router.include_router(admin_users.router)
router.include_router(admin_broadcast.router)

__all__ = ["router"]
