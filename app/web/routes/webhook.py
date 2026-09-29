"""Inbound webhooks.

Two very different callers arrive here:

* ``POST {WEBHOOK_PATH}`` — Telegram pushes updates.  The path contains a
  shared secret, and the ``X-Telegram-Bot-Api-Secret-Token`` header is verified
  as well when present.
* ``POST /wg/webhook/{panel_id}`` — a WG-Guard node delivers signed lifecycle
  events (``X-WG-Signature: t=<unix>,v1=<hex hmac-sha256(secret, "<t>.<body>")>``).
  Deliveries are at-least-once, so ``payload["id"]`` is the dedupe key and any
  2xx acknowledges the delivery.

Neither endpoint is under the panel prefix and neither requires a session, so
they are registered directly on the app rather than through the router registry.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from aiogram.types import Update
from fastapi import Depends, FastAPI, Header, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.security import constant_time_compare, decrypt_secret
from app.db.models import Panel, Service, ServiceEvent, ServiceStatus, WebhookEvent
from app.db.session import get_db
from app.services.notifications import notifier

log = get_logger(__name__)

#: Reject a signature older than this (the contract suggests five minutes).
REPLAY_WINDOW_SECONDS = 300


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
async def handle_telegram_update(request: Request, secret: str) -> Response:
    if not settings.webhook_secret or not constant_time_compare(secret, settings.webhook_secret):
        log.warning("Rejected Telegram webhook with a wrong secret")
        return Response(status_code=403)

    header_token = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_token and not constant_time_compare(header_token, settings.webhook_secret):
        log.warning("Rejected Telegram webhook: bad secret token header")
        return Response(status_code=403)

    runtime = getattr(request.app.state, "runtime", None)
    dispatcher = getattr(request.app.state, "dispatcher", None)
    if dispatcher is None or runtime is None or runtime.bot is None:
        return Response(status_code=503)

    try:
        payload = await request.json()
    except Exception:
        return Response(status_code=400)

    try:
        update = Update.model_validate(payload, context={"bot": runtime.bot})
        await dispatcher.feed_update(runtime.bot, update)
    except Exception as exc:  # pragma: no cover - never fail the webhook itself
        log.exception("Failed to process a Telegram update: %s", exc)
        await notifier.report_error(exc, source="telegram-webhook", notify=False)
    return Response(status_code=200)


# ---------------------------------------------------------------------------
# WG-Guard
# ---------------------------------------------------------------------------
def verify_wg_signature(secret: str, header: str | None, body: bytes) -> bool:
    """Validate ``X-WG-Signature: t=<unix>,v1=<hex>`` against a replay window."""
    if not header or not secret:
        return False

    parts: dict[str, str] = {}
    for chunk in header.split(","):
        key, sep, value = chunk.partition("=")
        if sep:
            parts[key.strip()] = value.strip()

    timestamp, signature = parts.get("t"), parts.get("v1")
    if not timestamp or not signature:
        return False
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False
    if age > REPLAY_WINDOW_SECONDS:
        log.warning("WG-Guard webhook rejected: timestamp %.0fs outside the replay window", age)
        return False

    expected = hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def handle_wg_event(
    panel_id: int,
    request: Request,
    session: AsyncSession,
    *,
    signature: str | None,
    delivery: str | None,
    event_header: str | None,
) -> Response:
    panel = await session.get(Panel, panel_id)
    if panel is None or not panel.is_active:
        return Response(status_code=404)

    body = await request.body()
    secret = (
        (
            decrypt_secret(panel.webhook_secret_encrypted, purpose="webhook-secret")
            if getattr(panel, "webhook_secret_encrypted", None)
            else None
        )
        or decrypt_secret(panel.api_token_encrypted, purpose="panel-token")
        or ""
    )

    if not verify_wg_signature(secret, signature, body):
        log.warning("WG-Guard webhook signature rejected for panel %s", panel.name)
        return Response(status_code=401)

    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        return Response(status_code=400)

    event_id = str(payload.get("id") or delivery or "")
    event_type = str(payload.get("type") or event_header or "unknown")
    if not event_id:
        return Response(status_code=400)

    if await session.get(WebhookEvent, event_id) is not None:
        return Response(status_code=200)  # redelivery: already applied

    record = WebhookEvent(
        id=event_id,
        event_type=event_type,
        node_id=payload.get("node_id"),
        payload=payload,
    )
    session.add(record)

    try:
        await apply_wg_event(session, panel, event_type, payload)
        record.processed_at = now_utc()
    except Exception as exc:  # pragma: no cover - the node must still receive its 2xx
        log.exception("WG-Guard event %s failed: %s", event_type, exc)
        record.error = f"{type(exc).__name__}: {exc}"[:1000]
    await session.commit()
    return Response(status_code=200)


async def apply_wg_event(session: AsyncSession, panel: Panel, event_type: str, payload: dict) -> None:
    """Fold a node event into local state (and let the reminder jobs react)."""
    data = payload.get("data") or {}
    wg_user_id = data.get("user_id")

    service: Service | None = None
    if wg_user_id:
        service = (
            await session.execute(
                select(Service).where(Service.wg_user_id == str(wg_user_id), Service.panel_id == panel.id)
            )
        ).scalar_one_or_none()

    if service is not None:
        session.add(
            ServiceEvent(
                service_id=service.id,
                wg_user_id=service.wg_user_id,
                event_type=event_type,
                payload=payload,
            )
        )

    if service is not None:
        if event_type == "user.traffic_exceeded":
            service.status = ServiceStatus.TRAFFIC_EXCEEDED
            service.remote_status = "traffic_exceeded"
        elif event_type == "user.disabled":
            service.remote_status = "disabled"
        elif event_type == "user.enabled":
            service.remote_status = "active"
        elif event_type == "user.expired":
            service.remote_status = "expired"
            service.status = ServiceStatus.EXPIRED
        elif event_type == "device.deleted":
            from app.db.models import ServiceDevice

            device = (
                await session.execute(
                    select(ServiceDevice).where(
                        ServiceDevice.service_id == service.id,
                        ServiceDevice.wg_device_id == str(data.get("device_id", "")),
                    )
                )
            ).scalar_one_or_none()
            if device is not None:
                await session.delete(device)
    elif event_type == "node.started":
        from app.panels.manager import panel_manager

        await panel_manager.check_health(session, panel, force=True)

    await session.flush()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------
def register_webhooks(app: FastAPI) -> None:
    """Attach both inbound webhook endpoints to the ASGI app."""

    @app.post(settings.webhook_path, include_in_schema=False, status_code=200)
    async def telegram_webhook(request: Request) -> Response:  # pragma: no cover - prod path
        return await handle_telegram_update(request, settings.webhook_secret)

    @app.post("/wg/webhook/{panel_id}", include_in_schema=False, status_code=200)
    async def wg_webhook(
        panel_id: int,
        request: Request,
        session: AsyncSession = Depends(get_db),
        x_wg_signature: str | None = Header(default=None, alias="X-WG-Signature"),
        x_wg_delivery: str | None = Header(default=None, alias="X-WG-Delivery"),
        x_wg_event: str | None = Header(default=None, alias="X-WG-Event"),
    ) -> Response:  # pragma: no cover - prod path
        return await handle_wg_event(
            panel_id,
            request,
            session,
            signature=x_wg_signature,
            delivery=x_wg_delivery,
            event_header=x_wg_event,
        )

    log.info("Webhook endpoints: %s and /wg/webhook/{panel_id}", settings.webhook_path)


__all__ = ["apply_wg_event", "handle_telegram_update", "handle_wg_event", "register_webhooks", "verify_wg_signature"]
