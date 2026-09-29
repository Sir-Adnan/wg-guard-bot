"""Business logic layer.

Import from here rather than from the individual modules so that a future
refactor (splitting a service, adding a caching decorator, swapping an
implementation) stays invisible to the handlers and the web panel.
"""

from __future__ import annotations

from app.services.appearance import AppearanceStore, appearance
from app.services.audit import AuditService, audit
from app.services.broadcast import AUDIENCES, BroadcastService, broadcasts
from app.services.catalog import CatalogService, catalog
from app.services.categories import CategoryNode, CategoryService, categories
from app.services.delivery import DeliveryService, delivery
from app.services.discounts import DiscountService, discounts
from app.services.gifts import GiftResult, GiftService, gifts
from app.services.guides import GuideService, guides
from app.services.membership import MembershipService, membership
from app.services.notifications import Media, Notifier, notifier
from app.services.orders import OrderService, PriceQuote, order_service
from app.services.provisioning import ProvisioningService, ProvisionResult, provisioning
from app.services.receipts import ReceiptService, ReviewOutcome, receipt_service
from app.services.reports import Dashboard, ReportService, SalesSummary, reports
from app.services.settings_store import SettingsStore, app_settings
from app.services.texts import TextStore, texts
from app.services.tickets import TicketService, tickets
from app.services.users import UserService, UserStats, user_service

__all__ = [
    "AUDIENCES",
    "AppearanceStore",
    "AuditService",
    "BroadcastService",
    "CatalogService",
    "CategoryNode",
    "CategoryService",
    "Dashboard",
    "DeliveryService",
    "DiscountService",
    "GiftResult",
    "GiftService",
    "GuideService",
    "Media",
    "MembershipService",
    "Notifier",
    "OrderService",
    "PriceQuote",
    "ProvisionResult",
    "ProvisioningService",
    "ReceiptService",
    "ReportService",
    "ReviewOutcome",
    "SalesSummary",
    "SettingsStore",
    "TextStore",
    "TicketService",
    "UserService",
    "UserStats",
    "app_settings",
    "appearance",
    "audit",
    "broadcasts",
    "catalog",
    "categories",
    "delivery",
    "discounts",
    "gifts",
    "guides",
    "membership",
    "notifier",
    "order_service",
    "provisioning",
    "receipt_service",
    "reports",
    "texts",
    "tickets",
    "user_service",
]
