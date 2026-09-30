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
from app.services.catalog import catalog
from app.services.categories import MAX_DEPTH, categories
from app.services.ordering import next_sort_order
from app.web.deps import form_bool, form_dict, form_int, form_int_list, form_str
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


async def _form_values(session: AsyncSession, form: dict[str, Any]) -> dict[str, Any]:
    name = form_str(form, "name")
    if not name:
        raise ValidationError("نام دسته‌بندی نمی‌تواند خالی باشد.")

    icon = form_str(form, "icon")
    if icon and icon not in VALID_ICONS:
        raise ValidationError("آیکون انتخابی معتبر نیست.")

    parent_raw = form_str(form, "parent_id")
    parent_id = int(parent_raw) if parent_raw.isdigit() else None

    values: dict[str, Any] = {
        "name": name,
        "parent_id": parent_id,
        # خالی‌بودن این دو فیلد یعنی «پاک شود»، پس رشته خالی (نه None) برمی‌گردد.
        "description": form_str(form, "description"),
        "icon": icon,
        "is_active": form_bool(form, "is_active"),
        "is_visible": form_bool(form, "is_visible"),
    }

    # ترتیب دستی از کشیدن ردیف‌ها می‌آید. اگر فرم عددی نفرستاده باشد، تغییر
    # نمی‌کنیم؛ «تغییر نده» برای ویرایش ``None`` است و برای ساخت، مقدار آخر فهرست.
    if form_str(form, "sort_order"):
        values["sort_order"] = form_int(form, "sort_order", 0)
    else:
        values["sort_order"] = await next_sort_order(session, "categories", scope=parent_id)
    return values


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


def _plan_ids(form: Any) -> list[int]:
    """The ticked plans of the picker, as integers.

    Every checkbox is its own ``plan_ids`` field, so ``getlist`` carries the
    whole selection; joining the values into one string lets ``form_int_list``
    read that shape and a plain ``plan_ids=3,7`` alike.
    """
    getlist = getattr(form, "getlist", None)
    raw = getlist("plan_ids") if getlist is not None else []
    return form_int_list({"plan_ids": ",".join(str(value) for value in raw)}, "plan_ids")


async def _plans_now(session: AsyncSession, category_id: int) -> set[int]:
    """Ids of the plans attached to this category right now.

    Direct plans only (``include_descendants`` stays off), because that is
    exactly the set ``categories.set_plans`` moves.  The route reads it to tell
    «وصل شد» from «جدا شد» in the flash message.
    """
    rows = await catalog.list_plans(session, category_id=category_id, include_inactive=True, include_test=True)
    return {plan.id for plan in rows}


def _assignment_message(name: str, *, attached: int, detached: int) -> str:
    """What the operator's click did, in their own words."""
    if attached and detached:
        return f"{fa_digits(attached)} پلن به دستهٔ «{name}» وصل شد و {fa_digits(detached)} پلن جدا شد."
    if attached:
        return f"{fa_digits(attached)} پلن به دستهٔ «{name}» وصل شد."
    return f"{fa_digits(detached)} پلن از دستهٔ «{name}» جدا شد و به «بدون دسته» رفت."


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
            "plan_options": await categories.plan_options(session),
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
        values = await _form_values(session, form)
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
        values = await _form_values(session, form)
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


@router.post("/categories/{category_id}/plans")
async def set_category_plans(
    category_id: int,
    request: Request,
    staff: Staff = Depends(require_manager()),
    session: AsyncSession = Depends(get_db_session),
):
    """Attach the ticked plans to a category, and detach the plans left unticked.

    The picker posts the whole selection, so unticking a plan that already
    belongs here moves it to «بدون دسته».  One click can only change this
    category: plans of other categories are never touched.
    """
    form = await request.form()
    verify_csrf(request, form.get("csrf_token"))

    plan_ids = _plan_ids(form)
    chosen = set(plan_ids)

    try:
        node = await categories.get(session, category_id)
        name = node.name
        before = await _plans_now(session, category_id)
        changed = await categories.set_plans(session, category_id, plan_ids)
        await session.commit()
    except AppError as exc:
        return redirect(BASE, message=exc.message, level="danger")

    if not changed:
        return redirect(BASE, message=f"دستهٔ «{name}» بدون تغییر ماند؛ انتخاب‌ها همان وضعیت فعلی بود.")

    message = _assignment_message(name, attached=len(chosen - before), detached=len(before - chosen))
    return redirect(BASE, message=message)


__all__ = ["router"]
