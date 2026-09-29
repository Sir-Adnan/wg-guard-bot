"""SQLAlchemy ORM models.

Design notes
------------
* **Money** is always an ``Integer``/``BigInteger`` count of *Rial*.  Toman is a
  presentation concern handled in :mod:`app.core.money`.
* **Secrets** (panel API tokens, WireGuard private configs, subscription links)
  are stored encrypted via :mod:`app.core.security`; the ``*_encrypted`` columns
  never hold plaintext.
* **Timestamps** are timezone-aware UTC.  Display conversion happens in
  :mod:`app.core.jalali`.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, TimestampMixin


# ---------------------------------------------------------------------------
# Enumerations (stored as short VARCHARs — no native PG enum, so schema
# evolution stays a plain ALTER rather than a type rewrite)
# ---------------------------------------------------------------------------
def _enum(py_enum: type[enum.Enum], length: int = 32) -> Enum:
    return Enum(py_enum, native_enum=False, length=length, validate_strings=True)


class StaffRole(str, enum.Enum):
    OWNER = "owner"  # full access, can manage staff & panels
    ADMIN = "admin"  # everything except staff management
    SUPPORT = "support"  # receipts, tickets, read-only catalog


class OrderStatus(str, enum.Enum):
    DRAFT = "draft"  # created, not yet submitted
    PENDING_PAYMENT = "pending_payment"  # waiting for the customer to pay
    AWAITING_REVIEW = "awaiting_review"  # receipt uploaded, waiting for staff
    PAID = "paid"  # money confirmed, queued for provisioning
    PROVISIONING = "provisioning"  # talking to the WG-Guard node
    COMPLETED = "completed"  # service delivered
    FAILED = "failed"  # provisioning failed (retryable)
    CANCELED = "canceled"  # canceled by customer/staff
    REFUNDED = "refunded"
    EXPIRED = "expired"  # payment window elapsed


class OrderKind(str, enum.Enum):
    NEW = "new"
    RENEW = "renew"
    EXTRA_TRAFFIC = "extra_traffic"
    EXTRA_DEVICE = "extra_device"
    TEST = "test"


class PaymentMethod(str, enum.Enum):
    CARD = "card"  # card-to-card, manual review
    WALLET = "wallet"  # paid from the balance
    ADMIN = "admin"  # granted by an administrator
    GIFT = "gift"
    REFERRAL = "referral"


class PaymentKind(str, enum.Enum):
    DEPOSIT = "deposit"
    PURCHASE = "purchase"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    REFERRAL = "referral"
    GIFT = "gift"


class ReceiptStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ReceiptMedia(str, enum.Enum):
    PHOTO = "photo"
    DOCUMENT = "document"
    TEXT = "text"


class DiscountKind(str, enum.Enum):
    PERCENT = "percent"
    AMOUNT = "amount"


class TicketStatus(str, enum.Enum):
    OPEN = "open"
    ANSWERED = "answered"
    PENDING = "pending"  # waiting for the customer
    CLOSED = "closed"


class TicketSender(str, enum.Enum):
    USER = "user"
    STAFF = "staff"


class PanelHealth(str, enum.Enum):
    UNKNOWN = "unknown"
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"


class ServiceStatus(str, enum.Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    TRAFFIC_EXCEEDED = "traffic_exceeded"
    DELETED = "deleted"


class BroadcastStatus(str, enum.Enum):
    DRAFT = "draft"
    RUNNING = "running"
    DONE = "done"
    CANCELED = "canceled"
    FAILED = "failed"


class EventLevel(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Customers & staff
# ---------------------------------------------------------------------------
class User(Base, TimestampMixin):
    """A Telegram customer."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    last_name: Mapped[str | None] = mapped_column(String(128))
    language_code: Mapped[str | None] = mapped_column(String(16))

    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    block_reason: Mapped[str | None] = mapped_column(String(255))
    is_bot_blocked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    balance_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True)
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    referral_earnings_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)

    test_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    test_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONType, default=dict)

    orders: Mapped[list[Order]] = relationship(back_populates="user", lazy="raise")
    services: Mapped[list[Service]] = relationship(back_populates="user", lazy="raise")

    @property
    def display_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or (f"@{self.username}" if self.username else str(self.telegram_id))

    @property
    def mention(self) -> str:
        """Safe HTML mention for admin notifications."""
        return f'<a href="tg://user?id={self.telegram_id}">{_escape(self.display_name)}</a>'

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.id} tg={self.telegram_id}>"


class Staff(Base, TimestampMixin):
    """An operator: bot admin/support plus optional web-panel credentials."""

    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    role: Mapped[StaffRole] = mapped_column(_enum(StaffRole), default=StaffRole.ADMIN, nullable=False, index=True)

    # Web panel login (nullable for bot-only operators)
    login: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    receive_receipts: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(Text)

    @property
    def is_owner(self) -> bool:
        return self.role == StaffRole.OWNER

    @property
    def can_manage_settings(self) -> bool:
        return self.role in (StaffRole.OWNER, StaffRole.ADMIN)

    @property
    def can_manage_staff(self) -> bool:
        return self.role == StaffRole.OWNER

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Staff {self.id} {self.role.value} tg={self.telegram_id}>"


# ---------------------------------------------------------------------------
# WG-Guard panels
# ---------------------------------------------------------------------------
class Panel(Base, TimestampMixin):
    """One VPN node (multi-panel / multi-server support).

    ``kind`` selects the adapter from :mod:`app.panels.registry`, which is what
    lets a WG-Guard node and, later, a PasarGuard/Marzban/x-ui node live side by
    side without any change to the business logic.
    """

    __tablename__ = "panels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: Provider identifier, e.g. ``wg_guard``.  See ``app/panels/registry.py``.
    kind: Mapped[str] = mapped_column(String(32), default="wg_guard", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_url: Mapped[str] = mapped_column(String(255), nullable=False)
    #: Public origin used for subscription links; falls back to ``base_url``.
    subscription_base_url: Mapped[str | None] = mapped_column(String(255))
    api_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    #: Per-panel HMAC secret used to verify inbound webhook deliveries.
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    #: Free-form provider options (transport, TLS verification, extra headers…).
    options: Mapped[dict | None] = mapped_column(JSONType, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    max_services: Mapped[int | None] = mapped_column(Integer)
    capacity_note: Mapped[str | None] = mapped_column(String(255))

    default_interface_id: Mapped[str | None] = mapped_column(String(64))
    default_interface_name: Mapped[str | None] = mapped_column(String(64))

    health: Mapped[PanelHealth] = mapped_column(_enum(PanelHealth), default=PanelHealth.UNKNOWN, nullable=False)
    health_message: Mapped[str | None] = mapped_column(String(255))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    node_version: Mapped[str | None] = mapped_column(String(64))
    node_id: Mapped[str | None] = mapped_column(String(64))
    uptime_seconds: Mapped[int | None] = mapped_column(BigInteger)
    service_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    plans: Mapped[list[Plan]] = relationship(back_populates="panel", lazy="raise")
    services: Mapped[list[Service]] = relationship(back_populates="panel", lazy="raise")

    @property
    def public_origin(self) -> str:
        return (self.subscription_base_url or self.base_url).rstrip("/")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Panel {self.id} {self.name!r} {self.base_url}>"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
class PlanCategory(Base, TimestampMixin):
    """A node in the shop's category tree.

    ``parent_id`` makes the tree arbitrarily deep: دسته‌بندی ← زیردسته ←
    زیرِ‌زیردسته.  The bot walks it level by level; the panel renders it as an
    indented tree.  A category with no children shows its plans directly.
    """

    __tablename__ = "plan_categories"
    __table_args__ = (
        UniqueConstraint("parent_id", "name", name="uq_category_sibling_name"),
        Index("ix_categories_parent_sort", "parent_id", "sort_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("plan_categories.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Emoji-catalog key (e.g. ``fire``) resolved through :mod:`app.services.appearance`.
    icon: Mapped[str | None] = mapped_column(String(32))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Optional per-category banner shown above the plan list.
    banner_file_id: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)

    parent: Mapped[PlanCategory | None] = relationship(
        back_populates="children", remote_side="PlanCategory.id", lazy="joined"
    )
    children: Mapped[list[PlanCategory]] = relationship(
        back_populates="parent",
        lazy="selectin",
        cascade="all, delete-orphan",
        order_by="PlanCategory.sort_order",
    )
    plans: Mapped[list[Plan]] = relationship(back_populates="category_node", lazy="raise")

    @property
    def depth(self) -> int:
        """Best-effort depth for templates.

        Safe while the parent chain is eagerly loaded (``lazy="joined"`` covers
        one level).  Anything that must be exact — validation, cycle checks —
        uses :meth:`app.services.categories.CategoryService.depth_of`, which
        walks the chain with explicit awaits instead of lazy loads (a lazy
        chain raises ``MissingGreenlet`` under asyncio).
        """
        node, level = self.parent, 1
        seen = {self.id}
        while node is not None and node.id not in seen:
            seen.add(node.id)
            level += 1
            try:
                node = node.parent
            except Exception:  # pragma: no cover - lazy load outside a session
                break
        return level

    @property
    def is_leaf(self) -> bool:
        return not self.children

    def __repr__(self) -> str:  # pragma: no cover
        return f"<PlanCategory {self.id} {self.name!r}>"


class Guide(Base, TimestampMixin):
    """A connection / FAQ article shown to customers from the bot."""

    __tablename__ = "guides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    #: ``connect`` | ``faq`` | ``buy`` | ``other``
    section: Mapped[str] = mapped_column(String(24), default="connect", nullable=False, index=True)
    #: ``android`` | ``ios`` | ``windows`` | ``macos`` | ``linux`` | ``all``
    platform: Mapped[str] = mapped_column(String(24), default="all", nullable=False)
    body: Mapped[str] = mapped_column(Text, default="", nullable=False)
    media_file_id: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    views: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Guide {self.id} {self.title!r}>"


class GiftCode(Base, TimestampMixin):
    """A redeemable code: wallet credit, a free plan, or a percentage bonus."""

    __tablename__ = "gift_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    #: ``wallet`` (credit) | ``plan`` (free service) | ``percent`` (bonus on top-up)
    kind: Mapped[str] = mapped_column(String(16), default="wallet", nullable=False)
    #: Rial for ``wallet``, plan id for ``plan``, percentage for ``percent``.
    value: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    min_amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<GiftCode {self.code} {self.kind}>"


class GiftRedemption(Base):
    __tablename__ = "gift_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_id: Mapped[int] = mapped_column(ForeignKey("gift_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sa.func.now(), nullable=False)


class Plan(Base, TimestampMixin):
    """A sellable subscription package (volume + duration + price)."""

    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    panel_id: Mapped[int | None] = mapped_column(ForeignKey("panels.id", ondelete="SET NULL"))

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    #: Legacy free-text category kept for imports; the tree lives in ``category_id``.
    category: Mapped[str | None] = mapped_column(String(64))
    #: Node in the (possibly nested) category tree.
    category_id: Mapped[int | None] = mapped_column(ForeignKey("plan_categories.id", ondelete="SET NULL"), index=True)
    #: Bullet list of selling points shown on the plan card.
    features: Mapped[list | None] = mapped_column(JSONType, default=list)
    #: Highlighted in the catalog ("پیشنهاد ویژه").
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # -- terms ------------------------------------------------------------
    traffic_gb: Mapped[int | None] = mapped_column(Integer)  # null = unlimited
    duration_days: Mapped[int | None] = mapped_column(Integer)  # null = unlimited
    device_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    speed_limit_down_kbps: Mapped[int | None] = mapped_column(Integer)
    speed_limit_up_kbps: Mapped[int | None] = mapped_column(Integer)
    start_policy: Mapped[str] = mapped_column(String(24), default="first_connection", nullable=False)

    # -- pricing (Rial!) ---------------------------------------------------
    price_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    old_price_rial: Mapped[int | None] = mapped_column(BigInteger)
    cost_rial: Mapped[int | None] = mapped_column(BigInteger)

    # -- flags -------------------------------------------------------------
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_unlimited_stock: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    stock: Mapped[int | None] = mapped_column(Integer)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    badge: Mapped[str | None] = mapped_column(String(32))

    # -- WG-Guard binding --------------------------------------------------
    #: Plan id on the node.  Auto-created on first sale when empty.
    wg_plan_id: Mapped[str | None] = mapped_column(String(64))
    interface_id: Mapped[str | None] = mapped_column(String(64))
    #: Username template; supports {tg}, {id}, {n}, {rand}
    username_template: Mapped[str] = mapped_column(String(64), default="wg{tg}", nullable=False)

    sales_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)

    panel: Mapped[Panel | None] = relationship(back_populates="plans", lazy="joined")
    category_node: Mapped[PlanCategory | None] = relationship(lazy="joined")

    @property
    def is_unlimited_traffic(self) -> bool:
        return not self.traffic_gb

    @property
    def is_unlimited_time(self) -> bool:
        return not self.duration_days

    @property
    def discount_percent(self) -> int:
        if self.old_price_rial and self.old_price_rial > self.price_rial:
            return round((1 - self.price_rial / self.old_price_rial) * 100)
        return 0

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Plan {self.id} {self.name!r} {self.price_rial}rial>"


class DiscountCode(Base, TimestampMixin):
    """Coupon / discount code."""

    __tablename__ = "discount_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    kind: Mapped[DiscountKind] = mapped_column(_enum(DiscountKind), default=DiscountKind.PERCENT, nullable=False)
    #: percent (0-100) or an amount in Rial depending on ``kind``
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    max_discount_rial: Mapped[int | None] = mapped_column(BigInteger)
    min_amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    max_uses: Mapped[int | None] = mapped_column(Integer)
    per_user_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    used_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    applies_to_plans: Mapped[list | None] = mapped_column(JSONType)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DiscountCode {self.code}>"


class DiscountRedemption(Base, TimestampMixin):
    __tablename__ = "discount_redemptions"
    __table_args__ = (UniqueConstraint("code_id", "order_id", name="uq_redemption_order"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_id: Mapped[int] = mapped_column(ForeignKey("discount_codes.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class CardAccount(Base, TimestampMixin):
    """A card-to-card destination account."""

    __tablename__ = "card_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    card_number: Mapped[str] = mapped_column(String(32), nullable=False)
    holder_name: Mapped[str] = mapped_column(String(128), nullable=False)
    bank_name: Mapped[str | None] = mapped_column(String(64))
    iban: Mapped[str | None] = mapped_column(String(34))
    instructions: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    @property
    def masked(self) -> str:
        from app.core.security import mask_card

        return mask_card(self.card_number)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<CardAccount {self.masked} {self.holder_name!r}>"


class Channel(Base, TimestampMixin):
    """A channel/group used for mandatory membership."""

    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: ``@username`` or numeric ``-100...`` chat id
    chat_id: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    invite_link: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(16), default="channel", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Auto-approve join requests when the bot is an admin of the channel.
    auto_approve_joins: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    show_in_menu: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Channel {self.chat_id} {self.title!r}>"


# ---------------------------------------------------------------------------
# Orders / services
# ---------------------------------------------------------------------------
class Order(Base, TimestampMixin):
    """A purchase intent.  Becomes a :class:`Service` once provisioned."""

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_status_created", "status", "created_at"),
        Index("ix_orders_user_status", "user_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_code: Mapped[str] = mapped_column(String(24), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    panel_id: Mapped[int | None] = mapped_column(ForeignKey("panels.id", ondelete="SET NULL"))
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="SET NULL"))

    kind: Mapped[OrderKind] = mapped_column(_enum(OrderKind), default=OrderKind.NEW, nullable=False)
    status: Mapped[OrderStatus] = mapped_column(
        _enum(OrderStatus), default=OrderStatus.DRAFT, nullable=False, index=True
    )
    payment_method: Mapped[PaymentMethod | None] = mapped_column(_enum(PaymentMethod))

    # -- money (Rial) ------------------------------------------------------
    amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    discount_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    wallet_used_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    payable_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    discount_code_id: Mapped[int | None] = mapped_column(ForeignKey("discount_codes.id", ondelete="SET NULL"))
    is_free: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # -- plan snapshot (immutable after creation) --------------------------
    plan_name: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    traffic_gb: Mapped[int | None] = mapped_column(Integer)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    device_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    speed_limit_down_kbps: Mapped[int | None] = mapped_column(Integer)
    speed_limit_up_kbps: Mapped[int | None] = mapped_column(Integer)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # -- provisioning ------------------------------------------------------
    wg_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    wg_device_id: Mapped[str | None] = mapped_column(String(64))
    #: Stable key sent as ``Idempotency-Key``; guarantees exactly-once delivery.
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    provisioned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    payment_reference: Mapped[str | None] = mapped_column(String(128))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    #: Deadline for uploading a card-to-card receipt.
    payment_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict | None] = mapped_column(JSONType, default=dict)

    user: Mapped[User] = relationship(back_populates="orders", lazy="joined")
    plan: Mapped[Plan | None] = relationship(lazy="joined")
    panel: Mapped[Panel | None] = relationship(lazy="joined")
    service: Mapped[Service | None] = relationship(back_populates="orders", lazy="raise", foreign_keys=[service_id])

    # -- helpers -----------------------------------------------------------
    @property
    def is_open(self) -> bool:
        return self.status in (
            OrderStatus.DRAFT,
            OrderStatus.PENDING_PAYMENT,
            OrderStatus.AWAITING_REVIEW,
            OrderStatus.PAID,
            OrderStatus.PROVISIONING,
            OrderStatus.FAILED,
        )

    @property
    def payable_amount(self) -> int:
        """What the customer still has to pay after wallet/discount."""
        return max(self.payable_rial, 0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Order {self.order_code} {self.status.value} {self.amount_rial}rial>"


class Service(Base, TimestampMixin):
    """A provisioned WG-Guard user (what the customer calls "سرویس")."""

    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    panel_id: Mapped[int] = mapped_column(ForeignKey("panels.id", ondelete="RESTRICT"), nullable=False)
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id", ondelete="SET NULL"))
    origin_order_id: Mapped[int | None] = mapped_column(
        # ``orders`` and ``services`` reference each other; ``use_alter`` makes
        # SQLAlchemy emit this constraint as a separate ALTER TABLE so table
        # creation order stops mattering.
        ForeignKey("orders.id", ondelete="SET NULL", use_alter=True, name="fk_services_origin_order"),
        nullable=True,
    )

    wg_user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    wg_username: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Encrypted ``/sub/<token>`` relative path — a bearer capability.
    subscription_encrypted: Mapped[str | None] = mapped_column(Text)

    status: Mapped[ServiceStatus] = mapped_column(
        _enum(ServiceStatus), default=ServiceStatus.ACTIVE, nullable=False, index=True
    )
    #: Last status reported by the node (may differ from ``status``).
    remote_status: Mapped[str | None] = mapped_column(String(32))
    disable_reason: Mapped[str | None] = mapped_column(String(32))

    traffic_limit_bytes: Mapped[int | None] = mapped_column(BigInteger)
    traffic_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    device_limit: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    speed_limit_down_kbps: Mapped[int | None] = mapped_column(Integer)
    speed_limit_up_kbps: Mapped[int | None] = mapped_column(Integer)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    is_test: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Reminder bookkeeping (one message per milestone)
    notified_3d: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_1d: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_expired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_80pct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notified_100pct: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    note: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONType, default=dict)

    user: Mapped[User] = relationship(back_populates="services", lazy="joined")
    panel: Mapped[Panel] = relationship(back_populates="services", lazy="joined")
    plan: Mapped[Plan | None] = relationship(lazy="joined")
    orders: Mapped[list[Order]] = relationship(back_populates="service", lazy="raise", foreign_keys="Order.service_id")
    devices: Mapped[list[ServiceDevice]] = relationship(
        back_populates="service", lazy="selectin", cascade="all, delete-orphan"
    )

    @property
    def remaining_bytes(self) -> int | None:
        if self.traffic_limit_bytes is None:
            return None
        return max(self.traffic_limit_bytes - self.traffic_used_bytes, 0)

    @property
    def usage_percent(self) -> float:
        if not self.traffic_limit_bytes:
            return 0.0
        return min(self.traffic_used_bytes / self.traffic_limit_bytes * 100, 100.0)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Service {self.id} {self.wg_username!r} {self.status.value}>"


class ServiceDevice(Base, TimestampMixin):
    """One WireGuard peer belonging to a service."""

    __tablename__ = "service_devices"
    __table_args__ = (UniqueConstraint("service_id", "wg_device_id", name="uq_service_device"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True, nullable=False)
    wg_device_id: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(64), default="device-1", nullable=False)
    ipv4_address: Mapped[str | None] = mapped_column(String(64))
    #: Encrypted WireGuard configuration text.
    config_encrypted: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    rx_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tx_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_handshake_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    service: Mapped[Service] = relationship(back_populates="devices", lazy="raise")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ServiceDevice {self.id} {self.name!r}>"


class Payment(Base, TimestampMixin):
    """Append-only money ledger."""

    __tablename__ = "payments"
    __table_args__ = (Index("ix_payments_user_created", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    kind: Mapped[PaymentKind] = mapped_column(_enum(PaymentKind), nullable=False, index=True)
    method: Mapped[PaymentMethod | None] = mapped_column(_enum(PaymentMethod))
    #: Signed amount: positive credits the wallet, negative debits it.
    amount_rial: Mapped[int] = mapped_column(BigInteger, nullable=False)
    balance_after_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    reference: Mapped[str | None] = mapped_column(String(128))
    description: Mapped[str | None] = mapped_column(String(255))
    created_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Payment {self.id} {self.kind.value} {self.amount_rial}>"


class Receipt(Base, TimestampMixin):
    """A card-to-card payment receipt awaiting review."""

    __tablename__ = "receipts"
    __table_args__ = (Index("ix_receipts_status_created", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"))
    #: ``deposit`` (wallet top-up) or ``purchase`` (paying an order).
    purpose: Mapped[str] = mapped_column(String(16), default="purchase", nullable=False)

    amount_rial: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    media: Mapped[ReceiptMedia] = mapped_column(_enum(ReceiptMedia), default=ReceiptMedia.PHOTO, nullable=False)
    file_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    note: Mapped[str | None] = mapped_column(Text)
    tracking_code: Mapped[str | None] = mapped_column(String(64))
    payer_card_last4: Mapped[str | None] = mapped_column(String(8))
    paid_at_text: Mapped[str | None] = mapped_column(String(64))

    status: Mapped[ReceiptStatus] = mapped_column(
        _enum(ReceiptStatus), default=ReceiptStatus.PENDING, nullable=False, index=True
    )
    reviewed_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(lazy="joined")
    order: Mapped[Order | None] = relationship(lazy="joined")
    #: One row per staff message, so an approval edits *every* copy at once.
    messages: Mapped[list[ReceiptMessage]] = relationship(
        back_populates="receipt", lazy="selectin", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Receipt {self.code} {self.status.value}>"


class ReceiptMessage(Base):
    """Tracks the copy of a receipt sent to each reviewer's private chat."""

    __tablename__ = "receipt_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), index=True, nullable=False)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    is_media: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    edited: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    receipt: Mapped[Receipt] = relationship(back_populates="messages", lazy="raise")


# ---------------------------------------------------------------------------
# Support tickets
# ---------------------------------------------------------------------------
class Ticket(Base, TimestampMixin):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_code: Mapped[str] = mapped_column(String(16), unique=True, index=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False)
    subject: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    status: Mapped[TicketStatus] = mapped_column(
        _enum(TicketStatus), default=TicketStatus.OPEN, nullable=False, index=True
    )
    assigned_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    unread_for_staff: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unread_for_user: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)

    user: Mapped[User] = relationship(lazy="joined")
    messages: Mapped[list[TicketMessage]] = relationship(
        back_populates="ticket", lazy="raise", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Ticket {self.ticket_code} {self.status.value}>"


class TicketMessage(Base, TimestampMixin):
    __tablename__ = "ticket_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id", ondelete="CASCADE"), index=True, nullable=False)
    sender: Mapped[TicketSender] = mapped_column(_enum(TicketSender), nullable=False)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    text: Mapped[str | None] = mapped_column(Text)
    file_id: Mapped[str | None] = mapped_column(Text)
    file_type: Mapped[str | None] = mapped_column(String(16))
    delivered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    ticket: Mapped[Ticket] = relationship(back_populates="messages", lazy="raise")


# ---------------------------------------------------------------------------
# Configuration stored in the database
# ---------------------------------------------------------------------------
class Setting(Base):
    """Key/value application settings editable from the admin panel."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | int | float | bool | None] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Setting {self.key}>"


class BotText(Base):
    """Overridable bot copy (Persian by default)."""

    __tablename__ = "bot_texts"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<BotText {self.key}>"


class ButtonStyleConfig(Base, TimestampMixin):
    """Per-button colour + premium-emoji mapping.

    ``style`` maps to Bot API 9.4+ ``InlineKeyboardButton.style``
    (``primary`` / ``success`` / ``danger`` / ``link``); ``icon_custom_emoji_id``
    maps to ``icon_custom_emoji_id``.
    """

    __tablename__ = "button_styles"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    label: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    style: Mapped[str | None] = mapped_column(String(16))
    icon_custom_emoji_id: Mapped[str | None] = mapped_column(String(64))
    emoji_fallback: Mapped[str | None] = mapped_column(String(16))
    group: Mapped[str] = mapped_column(String(32), default="general", nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    #: Operators can hide a button without deleting the feature behind it.
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ButtonStyleConfig {self.key} {self.style}>"


class AuditLog(Base):
    """Who did what, from where."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created", "created_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    actor_label: Mapped[str | None] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity: Mapped[str | None] = mapped_column(String(32))
    entity_id: Mapped[str | None] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict | None] = mapped_column(JSONType)
    ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sa.func.now(), nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AuditLog {self.action}>"


class Broadcast(Base, TimestampMixin):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_by_staff_id: Mapped[int | None] = mapped_column(ForeignKey("staff.id", ondelete="SET NULL"))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    media_file_id: Mapped[str | None] = mapped_column(Text)
    media_type: Mapped[str | None] = mapped_column(String(16))
    buttons: Mapped[list | None] = mapped_column(JSONType)
    status: Mapped[BroadcastStatus] = mapped_column(
        _enum(BroadcastStatus), default=BroadcastStatus.DRAFT, nullable=False, index=True
    )
    audience: Mapped[str] = mapped_column(String(32), default="all", nullable=False)
    total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When set (and in the future) the campaign waits for the scheduler.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)


class WebhookEvent(Base):
    """Deduplicated inbound WG-Guard webhook deliveries."""

    __tablename__ = "webhook_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    node_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict | None] = mapped_column(JSONType)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=sa.func.now(), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class ServiceEvent(Base):
    """Lifecycle events observed for a service (expiry, quota, first connect)."""

    __tablename__ = "service_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    service_id: Mapped[int | None] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), index=True)
    wg_user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    payload: Mapped[dict | None] = mapped_column(JSONType)
    notified_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True
    )


class SystemEvent(Base):
    """Application-level log surfaced in the panel ("رویداد‌های سیستم")."""

    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    level: Mapped[EventLevel] = mapped_column(_enum(EventLevel), default=EventLevel.INFO, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(64), default="app", nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


__all__ = [
    "AuditLog",
    "BotText",
    "Broadcast",
    "BroadcastStatus",
    "ButtonStyleConfig",
    "CardAccount",
    "Channel",
    "DiscountCode",
    "DiscountKind",
    "DiscountRedemption",
    "EventLevel",
    "GiftCode",
    "GiftRedemption",
    "Guide",
    "Order",
    "OrderKind",
    "OrderStatus",
    "Panel",
    "PanelHealth",
    "Payment",
    "PaymentKind",
    "PaymentMethod",
    "Plan",
    "PlanCategory",
    "Receipt",
    "ReceiptMedia",
    "ReceiptMessage",
    "ReceiptStatus",
    "Service",
    "ServiceDevice",
    "ServiceEvent",
    "ServiceStatus",
    "Setting",
    "Staff",
    "StaffRole",
    "SystemEvent",
    "Ticket",
    "TicketMessage",
    "TicketSender",
    "TicketStatus",
    "User",
    "WebhookEvent",
]
