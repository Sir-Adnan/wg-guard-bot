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
12. ``reply``    — the optional physical keyboard (exact labels only)
13. ``errors``   — global handler + catch-all
"""

from __future__ import annotations

from contextlib import suppress

from aiogram import Router

from app.bot.handlers import (
    admin,
    gift,
    guides,
    purchase,
    receipts_user,
    reply_menu,
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
    # Last of the real routers: its text handler must only see a message that
    # nothing else wanted, and it still sits ahead of the catch-all.
    reply_menu,
)


def detach_router(router: Router) -> None:
    """Detach ``router`` from its current parent so it can be re-included.

    aiogram routers are single-parent by design and ``include_router`` is *not*
    idempotent: attaching the same router to a second parent raises
    ``RuntimeError: Router is already attached``. Because the leaves here are
    module-level singletons, that made a second ``build_root_router()`` in one
    process fail — which is exactly what a test suite, a hot reload or an
    embedded restart does.

    Only the router itself is detached, never its children: its own subtree must
    survive intact for the re-include to be meaningful. aiogram offers no public
    detach, so this clears the same private field that ``include_router`` sets.
    """
    parent = router.parent_router
    if parent is None:
        return
    with suppress(ValueError):
        parent.sub_routers.remove(router)
    router._parent_router = None


def build_root_router() -> Router:
    """Compose every feature router into one root router.

    Safe to call more than once per process: each module router is detached from
    a previous root before being included in the new one.
    """
    root = Router(name="root")
    for module in ROUTER_ORDER:
        detach_router(module.router)
        root.include_router(module.router)
    return root


__all__ = ["ROUTER_ORDER", "build_root_router", "detach_router"]
