"""Typed callback-data factories.

Using :class:`aiogram.filters.callback_data.CallbackData` instead of hand-built
strings gives us validation, IDE support and a hard 64-byte guarantee.  Prefixes
are kept short on purpose.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData


class NavCB(CallbackData, prefix="nv"):
    """Navigation between top-level screens."""

    to: str
    page: int = 0


class MenuCB(CallbackData, prefix="mn"):
    action: str
    arg: str = ""


class PlanCB(CallbackData, prefix="pl"):
    action: str  # view | buy | test | page
    plan_id: int = 0
    page: int = 0


class CatCB(CallbackData, prefix="ct"):
    """Category-tree navigation: ``open`` a node, or page through its plans."""

    action: str  # open | root | page | featured
    category_id: int = 0
    page: int = 1


class GuideCB(CallbackData, prefix="gd"):
    action: str  # section | read | page
    guide_id: int = 0
    section: str = ""
    page: int = 1


class GiftCB(CallbackData, prefix="gf"):
    action: str  # menu | redeem | list | claim
    code_id: int = 0


class BuyCB(CallbackData, prefix="by"):
    action: str  # method | card | wallet | confirm | cancel | retry
    order_id: int = 0


class OrderCB(CallbackData, prefix="or"):
    action: str
    order_id: int = 0


class ServiceCB(CallbackData, prefix="sv"):
    action: str  # view | config | qr | link | rotate | renew | devices | delete
    service_id: int = 0
    page: int = 0


class DeviceCB(CallbackData, prefix="dv"):
    action: str  # add | delete | rename | qr
    device_id: int = 0
    service_id: int = 0


class WalletCB(CallbackData, prefix="wl"):
    action: str  # menu | deposit | custom | history | page
    amount_rial: int = 0
    page: int = 0


class TestCB(CallbackData, prefix="ts"):
    action: str  # info | claim


class SupportCB(CallbackData, prefix="sp"):
    action: str  # menu | new | view | close | reply
    ticket_id: int = 0


class ReferralCB(CallbackData, prefix="rf"):
    action: str


class ReceiptCB(CallbackData, prefix="rc"):
    """Staff receipt review — the same callback is broadcast to every reviewer."""

    action: str  # approve | reject | reject_reason | view_user | message
    receipt_id: int = 0


class AdminCB(CallbackData, prefix="ad"):
    action: str
    target_id: int = 0
    page: int = 1


class PageCB(CallbackData, prefix="pg"):
    section: str
    page: int = 1


class ConfirmCB(CallbackData, prefix="cf"):
    """Generic yes/no for a server-side pending action."""

    action: str
    token: str = ""


__all__ = [
    "AdminCB",
    "BuyCB",
    "CatCB",
    "ConfirmCB",
    "DeviceCB",
    "GiftCB",
    "GuideCB",
    "MenuCB",
    "NavCB",
    "OrderCB",
    "PageCB",
    "PlanCB",
    "ReceiptCB",
    "ReferralCB",
    "ServiceCB",
    "SupportCB",
    "TestCB",
    "WalletCB",
]
