"""All bot routers, in dispatch order.

The order is intentional and documented so that adding a router is a one-line
change in a predictable place:

1. ``admin``     — staff callbacks/commands (most specific)
2. ``start``     — /start, navigation, profile, referral
3. ``shop``      — the category tree and the plan catalog
4. ``guides``    — tutorials / FAQ
5. ``purchase``  — payment method, invoice, provisioning
6. ``gift``      — redeem codes
7. ``receipts``  — card-to-card intake (photo/document/text)
8. ``services``  — my services and their actions
9. ``wallet``    — balance and top-ups
10. ``test``     — free trial
11. ``support``  — tickets
12. ``errors``   — global handler + catch-all
"""

from __future__ import annotations

from aiogram import Router

from app.bot.handlers import (
    admin,
    gift,
    guides,
    purchase,
    receipts_user,
    services,
    shop,
    start,
    support,
    test_service,
    wallet,
)

ROUTER_ORDER = (
    admin,
    start,
    shop,
    guides,
    purchase,
    gift,
    receipts_user,
    services,
    wallet,
    test_service,
    support,
)


def build_root_router() -> Router:
    """Compose every feature router into one root router."""
    root = Router(name="root")
    for module in ROUTER_ORDER:
        root.include_router(module.router)
    return root


__all__ = ["ROUTER_ORDER", "build_root_router"]
