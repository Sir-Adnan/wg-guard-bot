"""Free test service ("سرویس تست").

One per customer, with a configurable cooldown.  The plan is created
automatically from the shop settings the first time it is needed, so the owner
never has to remember to add a hidden plan.
"""

from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import MenuCB, NavCB, TestCB
from app.bot.menus import default_main_menu, test_offer
from app.bot.utils import answer_callback, show
from app.core.errors import AppError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import format_gb
from app.db.models import OrderKind, Plan, User
from app.services.orders import order_service
from app.services.settings_store import app_settings, test_service_enabled
from app.services.texts import texts

log = get_logger(__name__)
router = Router(name="test_service")

TEST_PLAN_NAME = "سرویس تست"


# ---------------------------------------------------------------------------
async def get_or_create_test_plan(session: AsyncSession) -> Plan:
    """The hidden, zero-priced plan backing the free trial."""
    plan = (
        (
            await session.execute(
                select(Plan).where(Plan.is_test.is_(True), Plan.is_active.is_(True)).order_by(Plan.id.asc())
            )
        )
        .scalars()
        .first()
    )
    if plan is not None:
        return plan

    panel_id = app_settings.get_int("test.panel_id", 0) or None
    plan = Plan(
        panel_id=panel_id if isinstance(panel_id, int) and panel_id > 0 else None,
        name=TEST_PLAN_NAME,
        description="سرویس تست رایگان برای آشنایی با کیفیت اتصال",
        category=None,
        traffic_gb=app_settings.get_int("test.traffic_gb", 1),
        duration_days=app_settings.get_int("test.duration_days", 1),
        device_limit=app_settings.get_int("test.device_limit", 1),
        price_rial=0,
        is_active=True,
        is_test=True,
        is_unlimited_stock=True,
        sort_order=0,
        username_template="test{tg}",
        note="این پلن به‌صورت خودکار ساخته شده است.",
    )
    session.add(plan)
    await session.flush()
    log.info("Created automatic test plan #%s", plan.id)
    return plan


def cooldown_remaining(user: User) -> timedelta | None:
    cooldown_days = app_settings.get_int("test.cooldown_days", 30)
    if cooldown_days <= 0 or user.test_used_at is None:
        return None
    ready_at = user.test_used_at + timedelta(days=cooldown_days)
    if ready_at <= now_utc():
        return None
    return ready_at - now_utc()


# ---------------------------------------------------------------------------
@router.callback_query(MenuCB.filter(F.action == "test"))
async def test_intro(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await _render(callback, session, user)


async def _render(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not test_service_enabled():
        await show(
            callback,
            await texts.get("test.disabled", session),
            keyboard=await default_main_menu(session),
        )
        return

    plan = await get_or_create_test_plan(session)
    remaining = cooldown_remaining(user)
    available = remaining is None

    body = await texts.get("test.title", session, volume=format_gb(plan.traffic_gb), days=str(plan.duration_days or 1))
    if not available and remaining is not None:
        body += "\n\n" + await texts.get("test.cooldown", session, days=str(max(remaining.days, 1)))
    await show(callback, body, keyboard=await test_offer(session, available=available))


@router.callback_query(TestCB.filter(F.action == "claim"))
async def claim_test(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    if not test_service_enabled():
        await callback.answer(await texts.get("test.disabled", session), show_alert=True)
        return

    remaining = cooldown_remaining(user)
    if remaining is not None:
        await callback.answer(
            await texts.get("test.cooldown", session, days=str(max(remaining.days, 1))), show_alert=True
        )
        return

    plan = await get_or_create_test_plan(session)
    try:
        order = await order_service.create(session, user, plan, kind=OrderKind.TEST, free=True)
    except AppError as exc:
        await callback.answer(exc.message[:190], show_alert=True)
        return

    user.test_used_at = now_utc()
    user.test_count = int(user.test_count) + 1
    await session.flush()

    await answer_callback(callback, "در حال ساخت…")
    await show(callback, await texts.get("test.success", session))

    from app.bot.handlers.purchase import finalize_order

    await finalize_order(callback, session, order)


@router.callback_query(MenuCB.filter(F.action == "main"))
async def back_main(callback: CallbackQuery, session: AsyncSession, user: User, staff=None) -> None:
    await answer_callback(callback)
    await show(
        callback,
        await texts.get("common.back_menu_hint", session),
        keyboard=await default_main_menu(session, is_staff=bool(staff)),
    )


@router.callback_query(NavCB.filter(F.to == "test"))
async def nav_test(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await _render(callback, session, user)


__all__ = ["cooldown_remaining", "get_or_create_test_plan", "router"]
