"""Every button the bot can draw must lead somewhere.

Two back buttons shipped pointing at callback actions no handler filtered on
(``pl:page`` on every plan card and ``ct:root`` on «همه سرویسها»).  Telegram
does not report that: the button simply spins for ever, and the customer
concludes the bot is broken.  These tests read the real dispatcher, so a payload
that nothing answers fails here instead of in production.

The check is structural rather than behavioural: it runs the first filter of
every registered callback handler — in this codebase that is the
``CallbackData`` filter — against a synthetic callback with that payload.
"""

from __future__ import annotations

import pytest
from aiogram import Router
from aiogram.filters.callback_data import CallbackQueryFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery
from aiogram.types import User as TelegramUser

from app.bot import nav
from app.bot.handlers import build_root_router, errors
from app.bot.setup import create_dispatcher, reset_dispatcher

pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def _clean_dispatcher():
    reset_dispatcher()
    yield
    reset_dispatcher()


def _handlers(router: Router):
    yield from router.callback_query.handlers
    for child in router.sub_routers:
        yield from _handlers(child)


def _callback(payload: str) -> CallbackQuery:
    return CallbackQuery(
        id="1",
        from_user=TelegramUser(id=1, is_bot=False, first_name="tester"),
        chat_instance="chat-instance",
        data=payload,
    )


async def _is_answered(payload: str) -> bool:
    """Does a handler filter accept this payload?"""
    query = _callback(payload)
    for handler in _handlers(build_root_router()):
        for filter_object in handler.filters:
            if not isinstance(filter_object.callback, CallbackQueryFilter):
                continue
            try:
                if await filter_object.call(query):
                    return True
            except Exception:  # pragma: no cover - a broken filter is not a match
                continue
    return False


#: Every payload :mod:`app.bot.nav` can hand to a «بازگشت» button.
NAV_PAYLOADS: tuple[str, ...] = (
    nav.main(),
    nav.wallet(),
    nav.profile(),
    nav.gift(),
    nav.test_offer(),
    nav.shop_home(),
    nav.shop_all(),
    nav.shop_all(3),
    nav.shop_featured(),
    nav.shop_category(7),
    nav.shop_category(7, 2),
    nav.plan_parent(category_id=7, page=2),
    nav.plan_parent(category_id=None),
    nav.services(),
    nav.services(4),
    nav.service(11, 2),
    nav.guides_root(),
    nav.guide_section("android"),
    nav.support(),
    nav.tickets(),
    nav.ticket(3),
    nav.wallet_history(2),
)


@pytest.mark.parametrize("payload", NAV_PAYLOADS, ids=lambda value: value)
async def test_every_navigation_payload_is_answered(payload: str) -> None:
    assert await _is_answered(payload), f"no handler answers {payload!r} — the button would only spin"


async def test_every_main_menu_button_is_answered() -> None:
    from app.bot.callbacks import AdminCB, MenuCB
    from app.bot.menus import MAIN_MENU_LAYOUT

    for visual_key, (action, arg) in MAIN_MENU_LAYOUT:
        payload = MenuCB(action=action, arg=arg).pack()
        assert await _is_answered(payload), f"main-menu button {visual_key} ({payload!r}) is dead"

    assert await _is_answered(MenuCB(action="channels").pack())
    assert await _is_answered(AdminCB(action="broadcast").pack())


async def test_every_screen_button_the_menus_build_is_answered() -> None:
    """The keyboards menus.py builds must not contain a dead button either.

    Built without a session (no appearance overrides, no database), which is
    exactly the payload shape that matters here.
    """
    from app.bot.callbacks import PlanCB, ServiceCB
    from app.db.models import Plan, Service, ServiceStatus

    plan = Plan(id=5, name="p1", traffic_gb=30, duration_days=30, price_rial=1_000, category_id=2)
    service = Service(id=9, user_id=1, panel_id=1, plan_id=5, wg_user_id="u", wg_username="wg1")
    service.status = ServiceStatus.ACTIVE

    keyboards = (
        await nav_plan_list(plan),
        await nav_plan_actions(plan),
        await nav_service_list(service),
        await nav_service_detail(service),
    )
    payloads = [button.callback_data for kb in keyboards for row in kb.markup.inline_keyboard for button in row]

    assert payloads, "the builders produced an empty keyboard"
    for payload in payloads:
        assert payload is not None and await _is_answered(payload), f"dead button: {payload!r}"

    # The plan card's own button must carry the page it came from.
    assert PlanCB(action="view", plan_id=5, page=2).pack() in [
        button.callback_data for row in (await nav_plan_list(plan, page=2)).markup.inline_keyboard for button in row
    ]
    assert ServiceCB(action="list", page=2).pack() in [
        button.callback_data
        for row in (await nav_service_detail(service, page=2)).markup.inline_keyboard
        for button in row
    ]


async def nav_plan_list(plan, page: int = 1):
    from app.bot.menus import plan_list

    return await plan_list(None, [plan], page, 3, category_id=2, back=nav.shop_category(2))


async def nav_plan_actions(plan):
    from app.bot.menus import plan_actions

    return await plan_actions(None, plan, back=nav.shop_category(2))


async def nav_service_list(service):
    from app.bot.menus import service_list

    return await service_list(None, [service], 1, 2)


async def nav_service_detail(service, page: int = 1):
    from app.bot.menus import service_detail

    return await service_detail(None, service, page=page)


async def test_the_stale_button_net_is_registered_last() -> None:
    """A payload nobody claims must still be answered, never ignored."""
    catch_all = [handler for handler in errors.router.callback_query.handlers if not handler.filters]
    assert catch_all, "no filter-less callback handler: unhandled buttons would spin for ever"


async def test_the_stale_button_net_is_the_last_router_on_the_dispatcher() -> None:
    dispatcher = create_dispatcher(MemoryStorage())
    root = dispatcher.sub_routers[-1]
    assert root is errors.router, "the catch-all must be reachable only after every real router"


async def test_a_payload_no_handler_knows_is_reported_as_unanswered() -> None:
    """Guard the guard: the helper must not answer `True` for anything."""
    assert await _is_answered("zz:definitely-not-a-real-action") is False
