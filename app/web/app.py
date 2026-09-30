"""FastAPI application factory for the admin panel and the Telegram webhook."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __commit__, __version__
from app.core.cache import cache
from app.core.config import settings
from app.core.logging import get_logger
from app.web.routes import iter_routers
from app.web.templating import STATIC_DIR, render

log = get_logger(__name__)

COUNTERS_TTL = 8.0


@dataclass(slots=True)
class LayoutCounters:
    receipts: int = 0
    tickets: int = 0
    orders: int = 0


async def _layout_counters() -> tuple[LayoutCounters, int]:
    """Badge counts for the sidebar, cached for a few seconds."""
    cached = await cache.misc_cache.get("layout:counters")
    if cached is not None:
        counters, offline = cached
        return counters, offline

    counters = LayoutCounters()
    offline = 0
    try:
        from sqlalchemy import func, select

        from app.db.models import Order, OrderStatus, Panel, PanelHealth, Receipt, ReceiptStatus, Ticket, TicketStatus
        from app.db.session import session_scope

        async with session_scope() as session:
            counters.receipts = int(
                await session.scalar(select(func.count(Receipt.id)).where(Receipt.status == ReceiptStatus.PENDING)) or 0
            )
            counters.tickets = int(
                await session.scalar(select(func.count(Ticket.id)).where(Ticket.status != TicketStatus.CLOSED)) or 0
            )
            counters.orders = int(
                await session.scalar(select(func.count(Order.id)).where(Order.status == OrderStatus.AWAITING_REVIEW))
                or 0
            )
            offline = int(
                await session.scalar(
                    select(func.count(Panel.id)).where(Panel.is_active.is_(True), Panel.health == PanelHealth.OFFLINE)
                )
                or 0
            )
    except Exception as exc:  # pragma: no cover - the panel must render even if the DB hiccups
        log.debug("Layout counters unavailable: %s", exc)

    await cache.misc_cache.set("layout:counters", (counters, offline), ttl=COUNTERS_TTL)
    return counters, offline


def create_app(*, start_background: bool = True) -> FastAPI:
    """Build the ASGI app.  ``start_background`` controls the bot + scheduler."""
    from app.web.lifespan import build_lifespan

    app = FastAPI(
        title=f"{settings.app_name} — Admin API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=f"{settings.panel_prefix}/api/openapi.json",
        lifespan=build_lifespan() if start_background else None,
    )

    # -- static files ------------------------------------------------------
    static_dir = Path(STATIC_DIR)
    if static_dir.is_dir():
        app.mount(
            f"{settings.panel_prefix}/static",
            StaticFiles(directory=str(static_dir)),
            name="panel-static",
        )

    # -- layout context ----------------------------------------------------
    @app.middleware("http")
    async def attach_layout_context(request: Request, call_next):
        if request.url.path.startswith(settings.panel_prefix):
            counters, offline = await _layout_counters()
            request.state.counters = counters
            request.state.panels_offline = offline
        return await call_next(request)

    # -- health (never requires auth) --------------------------------------
    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        # ``commit`` answers "which code is live?" without a shell in the
        # container — the first thing to check when a fix seems to have no effect.
        return JSONResponse(
            {
                "status": "ok",
                "version": __version__,
                "commit": __commit__ or "unknown",
                "env": settings.env,
            }
        )

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        from app.db.session import ping

        ok = await ping()
        return JSONResponse({"status": "ready" if ok else "degraded"}, status_code=200 if ok else 503)

    # -- panel pages -------------------------------------------------------
    for router in iter_routers():
        app.include_router(router, prefix=settings.panel_prefix)

    # -- public webhooks (not under the panel prefix) ----------------------
    from app.web.routes.webhook import register_webhooks

    register_webhooks(app)

    # -- error pages -------------------------------------------------------
    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers and "location" in {k.lower() for k in exc.headers}:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith(settings.panel_prefix) and exc.status_code in (403, 404, 500):
            request.state.staff = None
            return render(
                request,
                "error.html",
                {"code": exc.status_code, "detail": exc.detail, "page_title": "خطا"},
                status_code=exc.status_code,
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):  # pragma: no cover
        log.exception("Unhandled panel error on %s", request.url.path)
        from app.services.notifications import notifier

        await notifier.report_error(exc, source="web")
        if request.url.path.startswith(settings.panel_prefix):
            return render(
                request,
                "error.html",
                {"code": 500, "detail": "خطای داخلی سرور", "page_title": "خطا"},
                status_code=500,
            )
        return JSONResponse({"detail": "internal error"}, status_code=500)

    log.info("Panel mounted at %s", settings.panel_prefix)
    return app


@asynccontextmanager
async def _noop_lifespan(app: FastAPI):  # pragma: no cover - convenience for tests
    yield


__all__ = ["LayoutCounters", "create_app"]
