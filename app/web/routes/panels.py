"""Panel management — the node inventory behind every sale.

This page is deliberately provider-agnostic: the "type" dropdown is generated
from :mod:`app.panels.registry`, so a new backend shows up here as soon as its
adapter is registered.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ConflictError, ValidationError
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.core.security import encrypt_secret, mask_secret
from app.db.models import Panel, PanelHealth, Service, ServiceStatus, Staff
from app.panels import registry
from app.panels.manager import panel_manager
from app.services.audit import audit
from app.services.ordering import next_sort_order
from app.web.deps import form_bool, form_dict, form_int, form_str
from app.web.security import get_db_session, require_manager
from app.web.templating import redirect, render

log = get_logger(__name__)
router = APIRouter(tags=["panels"])

BASE = f"{settings.panel_prefix}/panels"

#: Health values offered as a manual override (the probe normally owns this).
HEALTH_CHOICES: tuple[tuple[str, str], ...] = (
    ("unknown", "نامشخص"),
    ("online", "آنلاین"),
    ("degraded", "نیمه‌فعال"),
    ("offline", "آفلاین"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _get(session: AsyncSession, panel_id: int) -> Panel:
    panel = await session.get(Panel, panel_id)
    if panel is None:
        raise ValidationError("پنل مورد نظر پیدا نشد.")
    return panel


def _parse_options(raw: str) -> dict[str, Any]:
    """Provider options are a free-form JSON object; keep them honest."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise ValidationError("گزینه‌های پیشرفته باید JSON معتبر باشند.") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("گزینه‌های پیشرفته باید یک شیء JSON باشند.")
    return parsed


def _submitted_sort_order(form: dict[str, Any]) -> int | None:
    """``sort_order`` only when the form carried one; dragging owns it otherwise."""
    raw = form.get("sort_order")
    if raw is None or form_str(form, "sort_order") == "":
        return None
    return form_int(form, "sort_order", 0)


async def _payload(session: AsyncSession, form: dict[str, Any], *, creating: bool) -> dict[str, Any]:
    name = form_str(form, "name")
    base_url = form_str(form, "base_url").rstrip("/")
    if not name:
        raise ValidationError("نام پنل نمی‌تواند خالی باشد.")
    if not base_url.startswith(("http://", "https://")):
        raise ValidationError("آدرس پنل باید با http:// یا https:// شروع شود.")

    kind = form_str(form, "kind", registry.DEFAULT_KIND)
    if not registry.has(kind):
        raise ValidationError("نوع پنل انتخابی پشتیبانی نمی‌شود.")

    token = form_str(form, "api_token")
    if creating and not token:
        raise ValidationError("توکن API اجباری است.")

    data: dict[str, Any] = {
        "kind": kind,
        "name": name[:128],
        "base_url": base_url[:255],
        "subscription_base_url": (form_str(form, "subscription_base_url").rstrip("/") or None),
        "default_interface_id": form_str(form, "default_interface_id") or None,
        "max_services": form_int(form, "max_services", 0) or None,
        # اولویت انتخابِ پنل عددی می‌ماند: عدد بزرگ‌تر زودتر انتخاب می‌شود و
        # ربطی به ترتیب نمایش فهرست (که با کشیدن ردیف‌ها تعیین می‌شود) ندارد.
        "priority": form_int(form, "priority", 100),
        "capacity_note": form_str(form, "capacity_note") or None,
        "note": form_str(form, "note") or None,
        "is_active": form_bool(form, "is_active"),
        "options": _parse_options(form_str(form, "options")),
    }

    submitted = _submitted_sort_order(form)
    if submitted is not None:
        data["sort_order"] = submitted
    elif creating:
        data["sort_order"] = await next_sort_order(session, "panels")

    if token:
        data["api_token_encrypted"] = encrypt_secret(token, purpose="panel-token")
    secret = form_str(form, "webhook_secret")
    if secret:
        data["webhook_secret_encrypted"] = encrypt_secret(secret, purpose="webhook-secret")
    return data


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@router.get("/panels")
async def list_panels(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    rows = list((await session.execute(select(Panel).order_by(Panel.sort_order.asc(), Panel.id.asc()))).scalars())
    snapshots = await panel_manager.list_snapshots(session)
    counts = {snap.id: snap.service_count for snap in snapshots}
    labels = {kind: label for kind, label, _description in registry.choices()}

    return render(
        request,
        "panels.html",
        {
            "page_title": "پنل‌های VPN",
            "page_subtitle": f"{fa_digits(len(rows))} پنل ثبت شده",
            "rows": rows,
            "counts": counts,
            "kind_labels": labels,
            "provider_choices": registry.choices(),
            "health_choices": HEALTH_CHOICES,
            "registered_providers": [registry.describe(kind) for kind in registry.kinds()],
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
@router.post("/panels")
async def create_panel(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    from app.web.security import verify_csrf

    verify_csrf(request, form.get("csrf_token"))

    try:
        data = await _payload(session, form, creating=True)
        if form_bool(form, "is_default"):
            for other in (await session.execute(select(Panel))).scalars():
                other.is_default = False
            data["is_default"] = True

        panel = Panel(**data)
        session.add(panel)
        await session.flush()
        await audit.record(
            session,
            "panel.create",
            actor=staff,
            entity="panel",
            entity_id=panel.id,
            description=f"{panel.name} ({panel.kind})",
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    panel_manager.forget_all()
    return redirect(BASE, message=f"پنل «{panel.name}» اضافه شد. با دکمه «بررسی اتصال» صحت آن را تأیید کنید.")


@router.post("/panels/{panel_id}")
async def update_panel(
    panel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    from app.web.security import verify_csrf

    verify_csrf(request, form.get("csrf_token"))

    try:
        panel = await _get(session, panel_id)
        old_kind = panel.kind
        data = await _payload(session, form, creating=False)

        if form_bool(form, "is_default"):
            for other in (await session.execute(select(Panel).where(Panel.id != panel_id))).scalars():
                other.is_default = False
            data["is_default"] = True

        for key, value in data.items():
            setattr(panel, key, value)

        # Clearing a field the operator emptied on purpose.
        if not form_str(form, "subscription_base_url"):
            panel.subscription_base_url = None
        if form_bool(form, "clear_webhook_secret"):
            panel.webhook_secret_encrypted = None

        await session.flush()
        await audit.record(
            session,
            "panel.update",
            actor=staff,
            entity="panel",
            entity_id=panel.id,
            description=f"{panel.name} ({panel.kind})",
            meta={"kind_changed": old_kind != panel.kind},
        )
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    panel_manager.forget(panel_id)
    return redirect(BASE, message=f"تغییرات پنل «{panel.name}» ذخیره شد.")


@router.post("/panels/{panel_id}/check")
async def check_panel(
    panel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    from app.web.security import verify_csrf

    verify_csrf(request, form.get("csrf_token"))

    panel = await session.get(Panel, panel_id)
    if panel is None:
        return redirect(BASE, message="پنل مورد نظر پیدا نشد.", level="danger")

    panel_manager.forget(panel_id)
    try:
        provider = await panel_manager.provider_for(panel)
        report = await provider.health()
        info = await provider.node_info() if report.online else None
    except AppError as exc:
        panel.health = PanelHealth.OFFLINE
        panel.health_message = exc.message[:255]
        await session.commit()
        return redirect(BASE, message=f"اتصال برقرار نشد: {exc.message}", level="danger")

    panel.health = PanelHealth(report.state)
    panel.health_message = report.message
    if info is not None:
        panel.node_version = info.version
        panel.node_id = info.node_id
        panel.uptime_seconds = info.uptime_seconds
        panel.default_interface_name = info.interfaces[0].name if info.interfaces else None
    await session.flush()
    await audit.record(
        session, "panel.check", actor=staff, entity="panel", entity_id=panel.id, description=report.state
    )
    await session.commit()

    if not report.online:
        return redirect(BASE, message=f"پنل در دسترس نیست: {report.message or '—'}", level="danger")

    interface_count = len(info.interfaces) if info else 0
    message = f"اتصال موفق بود — نسخه {report.version or '—'}، {fa_digits(interface_count)} اینترفیس شناسایی شد."
    level = "warning" if report.degraded else "success"
    return redirect(BASE, message=message, level=level)


@router.post("/panels/{panel_id}/toggle")
async def toggle_panel(
    panel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    from app.web.security import verify_csrf

    verify_csrf(request, form.get("csrf_token"))

    panel = await _get(session, panel_id)
    panel.is_active = not panel.is_active
    await session.flush()
    await audit.record(
        session,
        "panel.toggle",
        actor=staff,
        entity="panel",
        entity_id=panel.id,
        description="active" if panel.is_active else "inactive",
    )
    await session.commit()
    state = "فعال" if panel.is_active else "غیرفعال"
    return redirect(BASE, message=f"پنل «{panel.name}» {state} شد.")


@router.post("/panels/{panel_id}/delete")
async def delete_panel(
    panel_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    from app.web.security import verify_csrf

    verify_csrf(request, form.get("csrf_token"))

    try:
        panel = await _get(session, panel_id)
        in_use = int(
            await session.scalar(
                select(func.count(Service.id)).where(
                    Service.panel_id == panel.id, Service.status != ServiceStatus.DELETED
                )
            )
            or 0
        )
        if in_use:
            raise ConflictError(
                f"این پنل {fa_digits(in_use)} سرویس فعال دارد و قابل حذف نیست. "
                "ابتدا سرویس‌ها را منتقل یا حذف کنید، یا پنل را غیرفعال کنید."
            )
        name = panel.name
        await session.delete(panel)
        await session.flush()
        await audit.record(session, "panel.delete", actor=staff, entity="panel", entity_id=panel_id, description=name)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    panel_manager.forget(panel_id)
    return redirect(BASE, message=f"پنل «{name}» حذف شد.")


def masked(panel: Panel) -> str:
    """Never expose a stored token, not even its length."""
    return mask_secret(panel.api_token_encrypted)


__all__ = ["router"]
