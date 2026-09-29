"""Route registry.

Every module listed here must expose a module-level ``router`` (an
``APIRouter``).  The order only matters for route-shadowing: keep the catch-all
webhook handlers last.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator

from fastapi import APIRouter

ROUTE_MODULES: tuple[str, ...] = (
    "auth",
    "dashboard",
    "plans",
    "categories",
    "cards",
    "channels",
    "discounts",
    "gifts",
    "guides",
    "orders",
    "receipts",
    "services",
    "users",
    "tickets",
    "buttons",
    "panels",
    "texts",
    "settings",
    "reports",
    "broadcast",
    "logs",
    "staff",
)
# NOTE: ``webhook`` is intentionally absent — its endpoints live outside the
# panel prefix and are registered directly by ``app.web.app.create_app``.


def iter_routers() -> Iterator[APIRouter]:
    """Import and yield each registered router.

    A module that cannot be imported is reported and skipped rather than taking
    the whole panel down — one broken page must never cost the operator access
    to the other twenty.
    """
    from app.core.logging import get_logger

    log = get_logger(__name__)
    for name in ROUTE_MODULES:
        try:
            module = importlib.import_module(f"app.web.routes.{name}")
        except (ImportError, SyntaxError) as exc:
            log.error("Panel route module %r could not be loaded: %s", name, exc)
            continue
        router = getattr(module, "router", None)
        if isinstance(router, APIRouter):
            yield router
        else:
            log.error("Panel route module %r does not expose a `router`", name)


__all__ = ["ROUTE_MODULES", "iter_routers"]
