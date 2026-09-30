"""Where «بازگشت» goes.

Every screen in this bot is a pure function of its callback payload, so the back
button of a screen is simply *the payload of the screen that opened it*.  Those
payloads are built here, once, instead of being hand-packed inside a menu
function: that is how a back button silently drifted into "a payload no handler
answers" (the button that only spun) or into "always page one".

Rules for a new screen:

* pass its parent payload explicitly — ``await plan_list(..., back=nav.shop_all(page))``;
* never invent a callback action that no handler filters on.  If a screen needs
  a new destination, add a handler for it first (a stale button is answered by
  the catch-all in :mod:`app.bot.handlers.errors`, but it can only apologise).
"""

from __future__ import annotations

from app.bot.callbacks import (
    AdminCB,
    CatCB,
    GuideCB,
    MenuCB,
    NavCB,
    ServiceCB,
    SupportCB,
    WalletCB,
)

#: The payload a category's "sections" screen uses for the root level.
GUIDE_ROOT = "__root__"


def page_number(page: int | None) -> int:
    """Paginate from 1 even when a payload carried 0 or nothing."""
    return page if page and page > 0 else 1


# -- top level --------------------------------------------------------------
def main() -> str:
    return NavCB(to="main").pack()


def wallet() -> str:
    return NavCB(to="wallet").pack()


def profile() -> str:
    return MenuCB(action="profile").pack()


def admin() -> str:
    return AdminCB(action="menu").pack()


def test_offer() -> str:
    return MenuCB(action="test").pack()


def gift() -> str:
    return MenuCB(action="gift").pack()


# -- shop -------------------------------------------------------------------
def shop_home() -> str:
    """The root of the category tree (or the flat catalog when there is none)."""
    return CatCB(action="home").pack()


def shop_all(page: int = 1) -> str:
    return CatCB(action="all", page=page_number(page)).pack()


def shop_featured(page: int = 1) -> str:
    return CatCB(action="featured", page=page_number(page)).pack()


def shop_category(category_id: int, page: int = 1) -> str:
    return CatCB(action="open", category_id=category_id, page=page_number(page)).pack()


def plan_parent(*, category_id: int | None, page: int = 1) -> str:
    """The list a plan card was opened from: its category, or every plan."""
    if category_id:
        return shop_category(category_id, page)
    return shop_all(page)


# -- services ---------------------------------------------------------------
def services(page: int = 1) -> str:
    return ServiceCB(action="list", page=page_number(page)).pack()


def service(service_id: int, page: int = 1) -> str:
    return ServiceCB(action="view", service_id=service_id, page=page_number(page)).pack()


# -- guides / support -------------------------------------------------------
def guides_root() -> str:
    return GuideCB(action="section", section=GUIDE_ROOT).pack()


def guide_section(section: str) -> str:
    return GuideCB(action="section", section=section or GUIDE_ROOT).pack()


def support() -> str:
    return SupportCB(action="menu").pack()


def tickets() -> str:
    return SupportCB(action="list").pack()


def ticket(ticket_id: int) -> str:
    return SupportCB(action="view", ticket_id=ticket_id).pack()


# -- wallet -----------------------------------------------------------------
def wallet_history(page: int = 1) -> str:
    return WalletCB(action="history", page=page_number(page)).pack()


__all__ = [
    "GUIDE_ROOT",
    "admin",
    "gift",
    "guide_section",
    "guides_root",
    "main",
    "page_number",
    "plan_parent",
    "profile",
    "service",
    "services",
    "shop_all",
    "shop_category",
    "shop_featured",
    "shop_home",
    "support",
    "test_offer",
    "ticket",
    "tickets",
    "wallet",
    "wallet_history",
]
