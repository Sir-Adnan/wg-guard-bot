"""Finite-state-machine states.

Kept in one module so the whole conversation flow is readable at a glance and
state names can never collide.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class ShopStates(StatesGroup):
    """Browsing, buying and paying."""

    browsing_plans = State()
    plan_detail = State()
    choosing_method = State()
    entering_discount = State()
    entering_gift_code = State()
    awaiting_receipt = State()
    entering_tracking = State()
    entering_payer_card = State()
    entering_custom_traffic = State()


class WalletStates(StatesGroup):
    choosing_amount = State()
    entering_amount = State()


class ServiceStates(StatesGroup):
    listing = State()
    detail = State()
    devices = State()
    naming_device = State()
    entering_extra_traffic = State()


class SupportStates(StatesGroup):
    menu = State()
    entering_subject = State()
    entering_message = State()
    replying = State()


class AdminStates(StatesGroup):
    menu = State()
    searching_user = State()
    user_card = State()
    entering_broadcast = State()
    confirming_broadcast = State()
    entering_balance = State()
    entering_note = State()
    messaging_user = State()
    reject_reason = State()


class ReceiptStates(StatesGroup):
    choosing_purpose = State()
    entering_amount = State()


__all__ = [
    "AdminStates",
    "ReceiptStates",
    "ServiceStates",
    "ShopStates",
    "SupportStates",
    "WalletStates",
]
