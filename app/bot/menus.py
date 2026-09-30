"""Screen keyboards.

Each function returns a :class:`~app.bot.keyboards.KB`, which carries both the
styled markup and its plain twin.  Handlers therefore never build
``InlineKeyboardMarkup`` objects directly and the colour scheme stays in one
place (the appearance catalog + the admin panel).
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.bot import nav
from app.bot.callbacks import (
    AdminCB,
    BuyCB,
    CatCB,
    MenuCB,
    NavCB,
    PageCB,
    PlanCB,
    ReceiptCB,
    ServiceCB,
    SupportCB,
    TestCB,
    WalletCB,
)
from app.bot.keyboards import KB, KeyboardBuilder
from app.core.money import fa_digits
from app.db.models import Order, Plan, Service, ServiceStatus
from app.services.settings_store import app_settings

# -- main menu --------------------------------------------------------------
MAIN_MENU_LAYOUT: tuple[tuple[str, tuple[str, str]], ...] = (
    # (visual key, (callback action, callback arg))
    ("menu.buy", ("buy", "")),
    ("menu.my_services", ("services", "")),
    ("menu.wallet", ("wallet", "")),
    ("menu.test", ("test", "")),
    ("menu.support", ("support", "")),
    ("menu.guides", ("guides", "")),
    ("menu.profile", ("profile", "")),
    ("menu.referral", ("referral", "")),
    ("menu.gift", ("gift", "")),
    ("menu.rules", ("rules", "")),
)

#: Buttons hidden when their feature is switched off in the panel.
FEATURE_GATED: dict[str, str] = {
    "menu.test": "test",
    "menu.wallet": "wallet",
    "menu.guides": "guides",
    "menu.gift": "gift",
}


async def main_menu(
    session: AsyncSession | None,
    *,
    is_staff: bool = False,
    show_test: bool = True,
    show_guides: bool = True,
    show_gift: bool = True,
    show_wallet: bool = True,
    columns: int = 2,
) -> KB:
    enabled = {
        "test": show_test,
        "wallet": show_wallet,
        "guides": show_guides,
        "gift": show_gift,
    }
    kb = KeyboardBuilder(session=session, columns=columns)
    for visual_key, (action, arg) in MAIN_MENU_LAYOUT:
        feature = FEATURE_GATED.get(visual_key)
        if feature and not enabled.get(feature, True):
            continue
        await kb.add(visual_key, callback=MenuCB(action=action, arg=arg).pack())
    kb.row()
    await kb.add("menu.channels", callback=MenuCB(action="channels").pack())
    if is_staff:
        # Straight to the broadcast flow: pointing this at the admin menu made
        # the button look broken.
        await kb.add("admin.broadcast", callback=AdminCB(action="broadcast").pack(), new_row=True)
    return kb.build()


async def default_main_menu(session: AsyncSession | None, *, is_staff: bool = False) -> KB:
    """Main menu with every feature gate applied from the current settings."""
    from app.services.settings_store import test_service_enabled, wallet_enabled

    return await main_menu(
        session,
        is_staff=is_staff,
        show_test=test_service_enabled(),
        show_wallet=wallet_enabled(),
        show_guides=app_settings.get_bool("shop.guides_enabled", True),
        show_gift=app_settings.get_bool("shop.gift_enabled", True),
    )


async def back_to_main(session: AsyncSession | None) -> KB:
    kb = KeyboardBuilder(session=session)
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


# -- shop -------------------------------------------------------------------
async def category_list(
    session: AsyncSession | None,
    nodes: Sequence,
    *,
    page: int = 1,
    total_pages: int = 1,
    show_all: bool = True,
    show_featured: bool = False,
    back: str | None = None,
) -> KB:
    """Buttons for one level of the category tree.

    ``nodes`` may be :class:`~app.services.categories.CategoryNode` objects or
    plain :class:`~app.db.models.PlanCategory` rows.  ``back`` is the payload of
    the parent screen (see :mod:`app.bot.nav`) — never a guess.
    """
    kb = KeyboardBuilder(session=session, columns=2)
    for node in nodes:
        category = getattr(node, "category", node)
        total = getattr(node, "total_plans", None)
        label = category.name if not total else f"{category.name} ({fa_digits(total)})"
        await kb.add(
            "shop.category",
            text=label,
            emoji_key=category.icon or None,
            callback=CatCB(action="open", category_id=category.id).pack(),
        )
    if show_featured:
        await kb.add("shop.featured", callback=CatCB(action="featured").pack())
    if show_all:
        await kb.add("shop.all_plans", callback=nav.shop_all())
    if total_pages > 1:
        kb.row()
        if page > 1:
            await kb.add("common.prev_page", callback=CatCB(action="page", page=page - 1).pack())
        if page < total_pages:
            await kb.add("common.next_page", callback=CatCB(action="page", page=page + 1).pack())
    kb.row()
    await kb.add("menu.back", callback=back or nav.main())
    return kb.build()


async def guide_sections(session: AsyncSession | None, counts: dict[str, int]) -> KB:
    from app.bot.callbacks import GuideCB

    kb = KeyboardBuilder(session=session, columns=1)
    from app.services.guides import SECTION_LABELS

    for section, label in SECTION_LABELS.items():
        count = counts.get(section)
        if not count:
            continue
        await kb.add(
            "guide.section",
            text=f"{label} ({fa_digits(count)})",
            callback=GuideCB(action="section", section=section).pack(),
        )
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


async def guide_list(session: AsyncSession | None, items: Sequence, section: str, *, back: str | None = None) -> KB:
    from app.bot.callbacks import GuideCB

    kb = KeyboardBuilder(session=session, columns=1)
    for guide in items:
        await kb.add("guide.read", text=guide.title[:60], callback=GuideCB(action="read", guide_id=guide.id).pack())
    kb.row()
    await kb.add("menu.back", callback=back or nav.guides_root())
    return kb.build()


async def gift_menu(session: AsyncSession | None, *, available: bool = True) -> KB:
    from app.bot.callbacks import GiftCB

    kb = KeyboardBuilder(session=session, columns=1)
    if available:
        await kb.add("gift.redeem", callback=GiftCB(action="redeem").pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


async def plan_list(
    session: AsyncSession | None,
    plans: Sequence[Plan],
    page: int,
    total_pages: int,
    *,
    page_action: str = "page",
    category_id: int = 0,
    back: str | None = None,
) -> KB:
    """A list of plans with optional pagination inside the category tree.

    ``back`` is the payload of the screen that opened this list; each plan
    button carries the page so the plan card can offer the same one back.
    """
    kb = KeyboardBuilder(session=session, columns=1)
    for plan in plans:
        visual = "shop.test_plan" if plan.is_test else "shop.plan"
        await kb.add(
            visual,
            text=_plan_button_label(plan),
            callback=PlanCB(action="view", plan_id=plan.id, page=page).pack(),
        )
    if total_pages > 1:
        kb.row()
        if page > 1:
            await kb.add(
                "common.prev_page",
                callback=CatCB(action=page_action, category_id=category_id, page=page - 1).pack(),
            )
        if page < total_pages:
            await kb.add(
                "common.next_page",
                callback=CatCB(action=page_action, category_id=category_id, page=page + 1).pack(),
            )
    kb.row()
    await kb.add("menu.back", callback=back or nav.shop_home())
    return kb.build()


def _plan_button_label(plan: Plan) -> str:
    from app.core.money import format_amount, format_gb

    price = format_amount(plan.price_rial) if plan.price_rial else "رایگان"
    volume = format_gb(plan.traffic_gb)
    return f"{plan.name} | {volume} | {price}"


async def plan_actions(
    session: AsyncSession | None, plan: Plan, *, can_buy: bool = True, back: str | None = None
) -> KB:
    kb = KeyboardBuilder(session=session, columns=1)
    if can_buy and plan.is_active:
        await kb.add("shop.buy_now", callback=PlanCB(action="buy", plan_id=plan.id).pack())
    kb.row()
    # The plan card is reached from a category (or the full list); ``back`` says
    # which one, so «بازگشت» returns to that list instead of a dead payload.
    await kb.add("menu.back", callback=back or nav.shop_home())
    return kb.build()


async def payment_methods(session: AsyncSession | None, order: Order, *, card: bool, wallet: bool) -> KB:
    kb = KeyboardBuilder(session=session, columns=1)
    if wallet:
        await kb.add("buy.method_wallet", callback=BuyCB(action="wallet", order_id=order.id).pack())
    if card:
        await kb.add("buy.method_card", callback=BuyCB(action="card", order_id=order.id).pack())
    if app_settings.get_bool("payment.discount_enabled", True) and order.discount_code_id is None:
        await kb.add("buy.apply_discount", callback=BuyCB(action="discount", order_id=order.id).pack())
    kb.row()
    await kb.add("menu.cancel", callback=BuyCB(action="cancel", order_id=order.id).pack())
    return kb.build()


async def card_payment_actions(session: AsyncSession | None, order: Order) -> KB:
    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("buy.send_receipt", callback=BuyCB(action="send_receipt", order_id=order.id).pack())
    kb.row()
    await kb.add("menu.cancel", callback=BuyCB(action="cancel", order_id=order.id).pack())
    return kb.build()


async def order_actions(session: AsyncSession | None, order: Order) -> KB:
    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("buy.send_receipt", callback=BuyCB(action="send_receipt", order_id=order.id).pack())
    await kb.add("menu.cancel", callback=BuyCB(action="cancel", order_id=order.id).pack(), new_row=True)
    return kb.build()


# -- services ---------------------------------------------------------------
STATUS_DOT = {
    ServiceStatus.ACTIVE: "🟢",
    ServiceStatus.DISABLED: "⚪️",
    ServiceStatus.EXPIRED: "🔴",
    ServiceStatus.TRAFFIC_EXCEEDED: "🟠",
    ServiceStatus.DELETED: "⚫️",
}


async def service_list(
    session: AsyncSession | None,
    services: Sequence[Service],
    page: int,
    total_pages: int,
    *,
    back: str | None = None,
) -> KB:
    kb = KeyboardBuilder(session=session, columns=1)
    for service in services:
        dot = STATUS_DOT.get(service.status, "⚪️")
        label = f"{dot} {service.wg_username}"
        await kb.add(
            "service.manage",
            text=label,
            callback=ServiceCB(action="view", service_id=service.id, page=page).pack(),
        )
    if total_pages > 1:
        kb.row()
        if page > 1:
            await kb.add("common.prev_page", callback=ServiceCB(action="page", page=page - 1).pack())
        if page < total_pages:
            await kb.add("common.next_page", callback=ServiceCB(action="page", page=page + 1).pack())
    kb.row()
    await kb.add("menu.main", callback=back or nav.main())
    return kb.build()


async def service_detail(session: AsyncSession | None, service: Service, *, page: int = 1) -> KB:
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("service.config", callback=ServiceCB(action="config", service_id=service.id).pack())
    await kb.add("service.qr", callback=ServiceCB(action="qr", service_id=service.id).pack())
    await kb.add("service.sub_link", callback=ServiceCB(action="link", service_id=service.id).pack())
    await kb.add("service.renew", callback=ServiceCB(action="renew", service_id=service.id).pack())
    if service.device_limit and service.device_limit > 1:
        await kb.add("service.add_device", callback=ServiceCB(action="devices", service_id=service.id).pack())
    await kb.add("service.autorenew", callback=ServiceCB(action="autorenew", service_id=service.id).pack())
    await kb.add("service.rotate", callback=ServiceCB(action="rotate", service_id=service.id).pack())
    kb.row()
    # Back to the list *page* the customer came from, not always page one.
    await kb.add("menu.back", callback=ServiceCB(action="list", page=page).pack())
    return kb.build()


async def device_list(session: AsyncSession | None, service: Service) -> KB:
    from app.bot.callbacks import DeviceCB

    kb = KeyboardBuilder(session=session, columns=1)
    for device in sorted(service.devices, key=lambda d: d.id):
        await kb.add(
            "service.config",
            text=f"⬇️ {device.name}",
            callback=DeviceCB(action="qr", device_id=device.id, service_id=service.id).pack(),
        )
        await kb.add(
            "service.delete_device",
            callback=DeviceCB(action="delete", device_id=device.id, service_id=service.id).pack(),
        )
        kb.row()
    await kb.add("service.add_device", callback=DeviceCB(action="add", service_id=service.id).pack())
    kb.row()
    await kb.add("menu.back", callback=ServiceCB(action="view", service_id=service.id).pack())
    return kb.build()


# -- wallet -----------------------------------------------------------------
DEPOSIT_PRESETS_RIAL: tuple[int, ...] = (2_000_000, 5_000_000, 10_000_000, 20_000_000)


async def wallet_menu(session: AsyncSession | None) -> KB:
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("wallet.deposit", callback=WalletCB(action="deposit").pack())
    await kb.add("wallet.history", callback=WalletCB(action="history").pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


async def deposit_amounts(session: AsyncSession | None) -> KB:
    kb = KeyboardBuilder(session=session, columns=2)
    for amount in DEPOSIT_PRESETS_RIAL:
        await kb.add(
            "wallet.deposit",
            text=_deposit_label(amount),
            callback=WalletCB(action="confirm", amount_rial=amount).pack(),
        )
    kb.row()
    await kb.add("wallet.custom_amount", callback=WalletCB(action="custom").pack())
    await kb.add("menu.back", callback=NavCB(to="wallet").pack())
    return kb.build()


def _deposit_label(rial: int) -> str:
    from app.core.money import format_amount

    return f"➕ {format_amount(rial)}"


# -- test service -----------------------------------------------------------
async def test_offer(session: AsyncSession | None, *, available: bool) -> KB:
    kb = KeyboardBuilder(session=session, columns=1)
    if available:
        await kb.add("test.claim", callback=TestCB(action="claim").pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


# -- support ----------------------------------------------------------------
async def support_menu(session: AsyncSession | None) -> KB:
    kb = KeyboardBuilder(session=session, columns=1)
    await kb.add("support.new_ticket", callback=SupportCB(action="new").pack())
    await kb.add("support.my_tickets", callback=SupportCB(action="list").pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


async def ticket_actions(session: AsyncSession | None, ticket_id: int) -> KB:
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("support.reply", callback=SupportCB(action="reply", ticket_id=ticket_id).pack())
    await kb.add("support.close_ticket", callback=SupportCB(action="close", ticket_id=ticket_id).pack())
    kb.row()
    # Back to the ticket *list*: the support menu would hide the ticket the
    # customer is reading.
    await kb.add("menu.back", callback=nav.tickets())
    return kb.build()


# -- admin ------------------------------------------------------------------
async def admin_menu(session: AsyncSession | None, *, pending_receipts: int = 0) -> KB:
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("admin.stats", callback=AdminCB(action="stats").pack())
    receipt_label = f"🧾 در انتظار ({pending_receipts})" if pending_receipts else None
    await kb.add(
        "admin.receipts",
        text=receipt_label,
        callback=AdminCB(action="receipts", page=1).pack(),
    )
    await kb.add("admin.orders", callback=AdminCB(action="orders", page=1).pack())
    await kb.add("admin.users", callback=AdminCB(action="users", page=1).pack())
    await kb.add("admin.panels", callback=AdminCB(action="panels").pack())
    await kb.add("admin.broadcast", callback=AdminCB(action="broadcast").pack())
    await kb.add("admin.export", callback=AdminCB(action="export").pack())
    kb.row()
    await kb.add("menu.main", callback=NavCB(to="main").pack())
    return kb.build()


async def receipt_review(session: AsyncSession | None, receipt_id: int) -> KB:
    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add("receipt.approve", callback=ReceiptCB(action="approve", receipt_id=receipt_id).pack())
    await kb.add("receipt.reject", callback=ReceiptCB(action="reject", receipt_id=receipt_id).pack())
    await kb.add("receipt.view_user", callback=ReceiptCB(action="view_user", receipt_id=receipt_id).pack())
    await kb.add("receipt.message_user", callback=ReceiptCB(action="message", receipt_id=receipt_id).pack())
    return kb.build()


async def pagination(session: AsyncSession | None, *, section: str, page: int, has_prev: bool, has_next: bool) -> KB:
    kb = KeyboardBuilder(session=session, columns=2)
    if has_prev:
        await kb.add("common.prev_page", callback=PageCB(section=section, page=page - 1).pack())
    if has_next:
        await kb.add("common.next_page", callback=PageCB(section=section, page=page + 1).pack())
    return kb.build()


__all__ = [
    "DEPOSIT_PRESETS_RIAL",
    "MAIN_MENU_LAYOUT",
    "STATUS_DOT",
    "admin_menu",
    "back_to_main",
    "card_payment_actions",
    "deposit_amounts",
    "device_list",
    "main_menu",
    "order_actions",
    "pagination",
    "payment_methods",
    "plan_actions",
    "plan_list",
    "receipt_review",
    "service_detail",
    "service_list",
    "support_menu",
    "test_offer",
    "ticket_actions",
    "wallet_menu",
]
