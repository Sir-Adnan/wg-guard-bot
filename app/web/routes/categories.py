"""دسته‌بندی‌ها — درخت تودرتوی کاتالوگ (PlanCategory).

کل درخت با :meth:`app.services.categories.CategoryService.tree` خوانده و به
صورت ردیف‌های تودرتو (فرورفته) نمایش داده می‌شود.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.errors import AppError, ConflictError, ValidationError
from app.core.money import fa_digits
from app.db.models import PlanCategory, Staff
from app.services.appearance import CATALOG
from app.services.categories import MAX_DEPTH, categories
from app.web.deps import form_bool, form_dict, form_int, form_str
from app.web.security import get_db_session, require_manager, verify_csrf
from app.web.templating import redirect, render

router = APIRouter(tags=["categories"])

BASE = f"{settings.panel_prefix}/categories"


# ---------------------------------------------------------------------------
# کمک‌کننده‌ها
# ---------------------------------------------------------------------------
def emoji_children() -> list[tuple[str, str, str]]:
    """``[(کلید کوتاه, یونیکد, برچسب)]`` برای سلکت ایموجی."""
    out: list[tuple[str, str, str]] = []
    for key, visual in CATALOG.items():
        if visual.kind != "emoji":
            continue
        out.append((key.split(".", 1)[1], visual.emoji_fallback, visual.label))
    return out


EMOJI_CHILDREN = emoji_children()
EMOJI_CHARS = {key: char for key, char, _label in EMOJI_CHILDREN}

VALID_ICONS = {key for key, _char, _label in EMOJI_CHILDREN}


def _emojis() -> dict[str, str]:
    """``{کلید: یونیکد}`` برای رندر آیکون هر ردیف."""
    return EMOJI_CHARS


async def _options(session: AsyncSession) -> list[tuple[int, PlanCategory]]:
    return await categories.flatten(session, active_only=False)


def _form_values(form: dict[str, Any]) -> dict[str, Any]:
    name = form_str(form, "name")
    if not name:
        raise ValidationError("نام دسته‌بندی نمی‌تواند خالی باشد.")

    icon = form_str(form, "icon")
    if icon and icon not in VALID_ICONS:
        raise ValidationError("آیکون انتخابی معتبر نیست.")

    parent_raw = form_str(form, "parent_id")
    return {
        "name": name,
        "parent_id": int(parent_raw) if parent_raw.isdigit() else None,
        # خالی‌بودن این دو فیلد یعنی «پاک شود»، پس رشته خالی (نه None) برمی‌گردد.
        "description": form_str(form, "description"),
        "icon": icon,
        "sort_order": form_int(form, "sort_order", 0),
        "is_active": form_bool(form, "is_active"),
        "is_visible": form_bool(form, "is_visible"),
    }


async def _move_to_root(session: AsyncSession, node: PlanCategory) -> None:
    """بازگرداندن یک دسته‌بندی به سطح اول.

    ``categories.update()`` مقدار ``None`` را «تغییری نده» تفسیر می‌کند، پس
    انتقال به ریشه اینجا انجام می‌شود (با همان قاعده یکتایی نام در هر سطح).
    """
    if node.parent_id is None:
        return
    clash = await session.scalar(
        select(func.count(PlanCategory.id)).where(
            PlanCategory.parent_id.is_(None),
            PlanCategory.name == node.name,
            PlanCategory.id != node.id,
        )
    )
    if clash:
        raise ConflictError("دسته‌بندی با این نام در همین سطح وجود دارد.")
    node.parent_id = None
    await session.flush()


# ---------------------------------------------------------------------------
# نمایش
# ---------------------------------------------------------------------------
@router.get("/categories")
async def list_categories(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    nodes = await categories.tree(session)
    total = sum(1 for _depth, _row in await _options(session))
    return render(
        request,
        "categories.html",
        {
            "page_title": "دسته‌بندی‌ها",
            "page_subtitle": f"{fa_digits(total)} دسته‌بندی در {fa_digits(len(nodes))} شاخه اصلی",
            "nodes": nodes,
            "category_options": await _options(session),
            "emoji_choices": EMOJI_CHILDREN,
            "emoji_chars": _emojis(),
            "max_depth": MAX_DEPTH,
            "base_url": BASE,
        },
    )


# ---------------------------------------------------------------------------
# نوشتن
# ---------------------------------------------------------------------------
@router.post("/categories")
async def create_category(
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        values = _form_values(form)
        values["description"] = values["description"] or None
        values["icon"] = values["icon"] or None
        node = await categories.create(session, **values)
        name = node.name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"دسته‌بندی «{name}» ساخته شد.")


@router.post("/categories/{category_id}")
async def update_category(
    category_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        values = _form_values(form)
        parent_id = values.pop("parent_id")
        node = await categories.update(
            session,
            category_id,
            **values,
            **({"parent_id": parent_id} if parent_id is not None else {}),
        )
        if parent_id is None:
            await _move_to_root(session, node)
        name = node.name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    return redirect(BASE, message=f"دسته‌بندی «{name}» ذخیره شد.")


@router.post("/categories/{category_id}/move")
async def move_category(
    category_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    direction = form_int(form, "direction", 0)
    if direction not in (-1, 1):
        return redirect(BASE, message="جهت جابه‌جایی نامعتبر است.", level="danger")

    try:
        node = await categories.move(session, category_id, direction)
        name = node.name
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    where = "بالاتر" if direction < 0 else "پایین‌تر"
    return redirect(BASE, message=f"دسته‌بندی «{name}» یک پله {where} رفت.")


@router.post("/categories/{category_id}/delete")
async def delete_category(
    category_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    form = await form_dict(request)
    verify_csrf(request, form.get("csrf_token"))

    try:
        node = await categories.get(session, category_id)
        name = node.name
        moved = await categories.delete(session, category_id)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    tail = f" و {fa_digits(moved)} پلن به «بدون دسته» منتقل شد" if moved else ""
    return redirect(BASE, message=f"دسته‌بندی «{name}» و زیردسته‌هایش حذف شد{tail}.")


__all__ = ["router"]
