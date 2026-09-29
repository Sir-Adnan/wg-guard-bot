"""Order lifecycle (database only).

Payment confirmation and provisioning are deliberately *separated*: this module
never performs network I/O, so an order transition is always a short, atomic
transaction.  :mod:`app.services.provisioning` then runs in its own session.

State machine::

    DRAFT ─┐
           ├─> PENDING_PAYMENT ──(receipt approved / wallet)──> PAID ─> PROVISIONING ─> COMPLETED
           │            │                                                      │
           │            └──(deadline)──> EXPIRED                                └──> FAILED (retryable)
           └────────────┴──(user/staff)──> CANCELED ──> (wallet refunded)
                                                        REFUNDED
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import days_to_seconds, gb_to_bytes
from app.core.security import random_code
from app.db.models import (
    Order,
    OrderKind,
    OrderStatus,
    PaymentKind,
    PaymentMethod,
    Plan,
    Service,
    User,
)
from app.services.audit import audit
from app.services.discounts import discounts
from app.services.settings_store import app_settings
from app.services.users import user_service

log = get_logger(__name__)

#: Statuses that still occupy a slot in the customer's "unfinished orders".
OPEN_STATUSES = (
    OrderStatus.DRAFT,
    OrderStatus.PENDING_PAYMENT,
    OrderStatus.AWAITING_REVIEW,
    OrderStatus.PAID,
    OrderStatus.PROVISIONING,
    OrderStatus.FAILED,
)

PAID_STATUSES = (OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.COMPLETED)
REVENUE_STATUSES = (OrderStatus.COMPLETED,)


@dataclass(slots=True)
class PriceQuote:
    """Result of pricing a plan for a specific customer."""

    amount_rial: int
    discount_rial: int = 0
    wallet_used_rial: int = 0
    discount_code: str | None = None

    @property
    def payable_rial(self) -> int:
        return max(self.amount_rial - self.discount_rial - self.wallet_used_rial, 0)

    @property
    def is_free(self) -> bool:
        return self.payable_rial <= 0


class OrderService:
    """Creates and transitions orders."""

    # -- lookup ------------------------------------------------------------
    async def get(self, session: AsyncSession, order_id: int) -> Order:
        order = await session.get(Order, order_id)
        if order is None:
            raise NotFoundError("سفارش مورد نظر پیدا نشد.")
        return order

    async def get_by_code(self, session: AsyncSession, code: str) -> Order | None:
        return (
            await session.execute(select(Order).where(Order.order_code == code.strip().upper()))
        ).scalar_one_or_none()

    async def open_orders(self, session: AsyncSession, user_id: int) -> list[Order]:
        return list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.user_id == user_id, Order.status.in_(OPEN_STATUSES))
                    .order_by(Order.created_at.desc())
                )
            ).scalars()
        )

    async def open_order_count(self, session: AsyncSession, user_id: int) -> int:
        return int(
            await session.scalar(
                select(func.count(Order.id)).where(Order.user_id == user_id, Order.status.in_(OPEN_STATUSES))
            )
            or 0
        )

    async def list_for_user(self, session: AsyncSession, user_id: int, *, limit: int = 20) -> list[Order]:
        return list(
            (
                await session.execute(
                    select(Order).where(Order.user_id == user_id).order_by(Order.created_at.desc()).limit(limit)
                )
            ).scalars()
        )

    async def pending_receipt_order(self, session: AsyncSession, user_id: int) -> Order | None:
        return (
            (
                await session.execute(
                    select(Order)
                    .where(Order.user_id == user_id, Order.status == OrderStatus.AWAITING_REVIEW)
                    .order_by(Order.created_at.desc())
                )
            )
            .scalars()
            .first()
        )

    # -- creation ----------------------------------------------------------
    async def quote(
        self,
        session: AsyncSession,
        user: User,
        plan: Plan,
        *,
        discount_code: str | None = None,
        use_wallet: bool = False,
        free: bool = False,
    ) -> PriceQuote:
        """Price a plan **without side effects** (used for previews).

        Unlike :meth:`create` this validates the code and computes the reduction
        but does not consume a redemption.
        """
        quote = PriceQuote(amount_rial=int(plan.price_rial))
        if free:
            quote.discount_rial = quote.amount_rial
            return quote

        if discount_code and app_settings.get_bool("payment.discount_enabled", True):
            row = await discounts.validate(
                session, discount_code, user=user, amount_rial=quote.amount_rial, plan_id=plan.id
            )
            quote.discount_rial = discounts.compute(row, quote.amount_rial)
            quote.discount_code = row.code

        if use_wallet:
            remaining = quote.amount_rial - quote.discount_rial
            quote.wallet_used_rial = max(min(int(user.balance_rial), remaining), 0)
        return quote

    async def create(
        self,
        session: AsyncSession,
        user: User,
        plan: Plan,
        *,
        kind: OrderKind = OrderKind.NEW,
        service: Service | None = None,
        payment_method: PaymentMethod | None = None,
        discount_code: str | None = None,
        use_wallet: bool = False,
        free: bool = False,
        staff_id: int | None = None,
    ) -> Order:
        """Create an order in ``PENDING_PAYMENT`` (or ``PAID`` when nothing is owed)."""
        if not plan.is_active and not free:
            raise ValidationError("این پلن در حال حاضر قابل خریداری نیست.")
        if plan.is_test and not free and kind != OrderKind.TEST:
            raise ValidationError("پلن‌های تست از مسیر خرید عادی قابل تهیه نیستند.")

        max_open = app_settings.get_int("advanced.max_open_orders", 3)
        if await self.open_order_count(session, user.id) >= max_open and kind != OrderKind.TEST:
            raise ConflictError("شما چند سفارش باز دارید. ابتدا آن‌ها را تکمیل یا لغو کنید.")

        amount = int(plan.price_rial)
        discount_rial = 0
        code_row_id: int | None = None

        if free:
            discount_rial = amount
        elif discount_code and app_settings.get_bool("payment.discount_enabled", True):
            result = await discounts.apply(session, discount_code, user=user, amount_rial=amount, plan_id=plan.id)
            discount_rial = result.amount_rial
            code_row_id = result.code.id

        wallet_used = 0
        if use_wallet and not free:
            wallet_used = max(min(int(user.balance_rial), amount - discount_rial), 0)

        order = Order(
            order_code=await self._new_code(session),
            user_id=user.id,
            plan_id=plan.id,
            panel_id=plan.panel_id,
            service_id=service.id if service else None,
            kind=kind,
            payment_method=payment_method,
            amount_rial=amount,
            discount_rial=discount_rial,
            wallet_used_rial=wallet_used,
            payable_rial=max(amount - discount_rial - wallet_used, 0),
            discount_code_id=code_row_id,
            is_free=free,
            is_test=plan.is_test or kind == OrderKind.TEST,
            plan_name=plan.name,
            traffic_gb=plan.traffic_gb,
            duration_days=plan.duration_days,
            device_limit=plan.device_limit,
            speed_limit_down_kbps=plan.speed_limit_down_kbps,
            speed_limit_up_kbps=plan.speed_limit_up_kbps,
            idempotency_key=self._new_idempotency_key(),
            status=OrderStatus.PENDING_PAYMENT,
            payment_deadline=now_utc() + timedelta(minutes=app_settings.get_int("shop.receipt_expire_minutes", 90)),
            meta={"start_policy": plan.start_policy, "interface_id": plan.interface_id},
        )
        session.add(order)
        await session.flush()

        if code_row_id is not None:
            await self._attach_redemption(session, code_row_id, user.id, order.id)

        # Reserve the wallet share immediately so it cannot be double-spent.
        if wallet_used > 0:
            await user_service.debit(
                session,
                user,
                wallet_used,
                kind=PaymentKind.PURCHASE,
                method=PaymentMethod.WALLET,
                order_id=order.id,
                description=f"رزرو بخشی از مبلغ سفارش {order.order_code}",
            )

        if order.payable_rial <= 0:
            await self.mark_paid(
                session,
                order,
                method=PaymentMethod.ADMIN if free else PaymentMethod.WALLET,
                reference="free" if free else None,
                staff_id=staff_id,
            )

        await audit.record(
            session,
            "order.create",
            actor=staff_id,
            user_id=user.id,
            entity="order",
            entity_id=order.id,
            description=f"{order.order_code} / {plan.name}",
            meta={"amount_rial": amount, "payable_rial": order.payable_rial, "kind": kind.value},
        )
        log.info("Order %s created for %s (%s rial payable)", order.order_code, user.telegram_id, order.payable_rial)
        return order

    async def apply_discount_code(self, session: AsyncSession, order: Order, code: str) -> str:
        """Attach a discount code to an existing unpaid order.

        Returns the normalised code so the caller can echo it back to the user.
        """
        if order.status != OrderStatus.PENDING_PAYMENT:
            raise ConflictError("این سفارش در وضعیتی نیست که بتوان کد تخفیف روی آن اعمال کرد.")
        if order.discount_code_id is not None:
            raise ConflictError("برای این سفارش قبلاً یک کد تخفیف ثبت شده است.")

        buyer = await session.get(User, order.user_id)
        if buyer is None:
            raise NotFoundError("کاربر این سفارش پیدا نشد.")

        result = await discounts.apply(
            session,
            code,
            user=buyer,
            amount_rial=order.amount_rial,
            plan_id=order.plan_id,
            order_id=order.id,
        )
        order.discount_rial = result.amount_rial
        order.discount_code_id = result.code.id
        order.payable_rial = max(order.amount_rial - order.discount_rial - order.wallet_used_rial, 0)
        await session.flush()

        await audit.record(
            session,
            "order.discount",
            user_id=order.user_id,
            entity="order",
            entity_id=order.id,
            description=f"{order.order_code} <- {result.code.code}",
            meta={"discount_rial": result.amount_rial},
        )
        return result.code.code

    async def _attach_redemption(self, session: AsyncSession, code_id: int, user_id: int, order_id: int) -> None:
        from app.db.models import DiscountRedemption

        row = (
            (
                await session.execute(
                    select(DiscountRedemption)
                    .where(
                        DiscountRedemption.code_id == code_id,
                        DiscountRedemption.user_id == user_id,
                        DiscountRedemption.order_id.is_(None),
                    )
                    .order_by(DiscountRedemption.created_at.desc())
                )
            )
            .scalars()
            .first()
        )
        if row is not None:
            row.order_id = order_id
            await session.flush()

    @staticmethod
    def _new_idempotency_key() -> str:
        """Stable key reused across provisioning retries (never regenerated)."""
        from app.core.security import random_token

        return f"wggb-{random_token(24)}"

    async def _new_code(self, session: AsyncSession) -> str:
        for _ in range(12):
            code = f"WG-{random_code(6)}"
            exists = await session.scalar(select(func.count(Order.id)).where(Order.order_code == code))
            if not exists:
                return code
        from app.core.security import random_token

        return f"WG-{random_token(8).upper()}"

    # -- transitions -------------------------------------------------------
    async def mark_paid(
        self,
        session: AsyncSession,
        order: Order,
        *,
        method: PaymentMethod,
        reference: str | None = None,
        staff_id: int | None = None,
    ) -> Order:
        if order.status in (OrderStatus.COMPLETED, OrderStatus.PAID, OrderStatus.PROVISIONING):
            return order
        if order.status in (OrderStatus.CANCELED, OrderStatus.REFUNDED):
            raise ConflictError("این سفارش لغو شده است و قابل پرداخت نیست.")

        order.status = OrderStatus.PAID
        order.payment_method = method
        order.payment_reference = reference
        order.paid_at = now_utc()
        await session.flush()

        # The wallet ledger only records money that actually moves through the
        # wallet.  A card-to-card transfer arrives from the customer's bank, so
        # debiting the wallet here would either fail (no balance) or — worse —
        # silently double-charge a customer who also topped the wallet up.
        if order.payable_rial > 0 and method in (
            PaymentMethod.WALLET,
            PaymentMethod.ADMIN,
            PaymentMethod.GIFT,
        ):
            buyer = await session.get(User, order.user_id)
            if buyer is None:
                raise NotFoundError("کاربر این سفارش پیدا نشد.")
            if method is not PaymentMethod.WALLET or buyer.balance_rial >= order.payable_rial:
                await user_service.debit(
                    session,
                    buyer,
                    order.payable_rial,
                    kind=PaymentKind.PURCHASE,
                    method=method,
                    order_id=order.id,
                    description=f"پرداخت سفارش {order.order_code}",
                    reference=reference,
                    staff_id=staff_id,
                )
            else:
                # Wallet payment with an unexpected shortfall: fall back to the
                # reserved amount only, so the order still completes.
                await user_service.debit(
                    session,
                    buyer,
                    buyer.balance_rial,
                    kind=PaymentKind.PURCHASE,
                    method=method,
                    order_id=order.id,
                    description=f"پرداخت سفارش {order.order_code}",
                    reference=reference,
                    staff_id=staff_id,
                    allow_partial=True,
                )

        await audit.record(
            session,
            "order.paid",
            actor=staff_id,
            user_id=order.user_id,
            entity="order",
            entity_id=order.id,
            description=f"{order.order_code} paid via {method.value}",
            meta={"reference": reference},
        )
        log.info("Order %s marked paid (%s)", order.order_code, method.value)
        return order

    async def mark_awaiting_review(self, session: AsyncSession, order: Order) -> Order:
        if order.status == OrderStatus.PENDING_PAYMENT:
            order.status = OrderStatus.AWAITING_REVIEW
            await session.flush()
        return order

    async def mark_provisioning(self, session: AsyncSession, order: Order) -> Order:
        order.status = OrderStatus.PROVISIONING
        order.attempts = int(order.attempts) + 1
        order.failure_reason = None
        await session.flush()
        return order

    async def mark_failed(self, session: AsyncSession, order: Order, reason: str) -> Order:
        order.status = OrderStatus.FAILED
        order.failure_reason = reason[:1000]
        await session.flush()
        log.warning("Order %s provisioning failed: %s", order.order_code, reason)
        return order

    async def mark_completed(self, session: AsyncSession, order: Order, service: Service | None = None) -> Order:
        order.status = OrderStatus.COMPLETED
        order.completed_at = now_utc()
        order.failure_reason = None
        if service is not None:
            order.service_id = service.id
            order.wg_user_id = service.wg_user_id
            order.expires_at = service.expires_at
            order.panel_id = service.panel_id
        await session.flush()
        return order

    async def cancel(
        self,
        session: AsyncSession,
        order: Order,
        *,
        reason: str | None = None,
        refund_wallet: bool = True,
        staff_id: int | None = None,
    ) -> Order:
        if order.status in (OrderStatus.COMPLETED, OrderStatus.REFUNDED):
            raise ConflictError("سفارش تکمیل‌شده را نمی‌توان لغو کرد.")
        if order.status == OrderStatus.CANCELED:
            return order

        order.status = OrderStatus.CANCELED
        order.cancel_reason = reason
        await session.flush()

        if refund_wallet and order.wallet_used_rial > 0:
            user = await session.get(User, order.user_id)
            if user is not None:
                await user_service.credit(
                    session,
                    user,
                    order.wallet_used_rial,
                    kind=PaymentKind.REFUND,
                    method=PaymentMethod.WALLET,
                    order_id=order.id,
                    description=f"بازگشت وجه رزروشده سفارش {order.order_code}",
                )

        await audit.record(
            session,
            "order.cancel",
            actor=staff_id,
            user_id=order.user_id,
            entity="order",
            entity_id=order.id,
            description=reason or "canceled",
        )
        return order

    async def refund(
        self,
        session: AsyncSession,
        order: Order,
        *,
        amount_rial: int | None = None,
        reason: str,
        to_wallet: bool = True,
        staff_id: int | None = None,
    ) -> Order:
        """Return money to the customer (wallet by default)."""
        if order.status == OrderStatus.REFUNDED:
            raise ConflictError("این سفارش قبلاً بازگشت داده شده است.")

        total = int(amount_rial if amount_rial is not None else order.payable_rial)
        if total <= 0:
            raise ValidationError("مبلغ قابل بازگشت صفر است.")
        if not to_wallet:
            raise ValidationError("در حال حاضر فقط بازگشت به کیف پول پشتیبانی می‌شود.")

        user = await session.get(User, order.user_id)
        if user is None:
            raise NotFoundError("کاربر سفارش پیدا نشد.")

        await user_service.credit(
            session,
            user,
            total,
            kind=PaymentKind.REFUND,
            method=PaymentMethod.WALLET,
            order_id=order.id,
            description=f"بازگشت وجه سفارش {order.order_code}",
            staff_id=staff_id,
        )
        order.status = OrderStatus.REFUNDED
        order.cancel_reason = reason
        await session.flush()

        await audit.record(
            session,
            "order.refund",
            actor=staff_id,
            user_id=order.user_id,
            entity="order",
            entity_id=order.id,
            description=reason,
            meta={"amount_rial": total},
        )
        return order

    async def retry_provisioning(self, session: AsyncSession, order: Order, staff_id: int | None = None) -> Order:
        if order.status not in (OrderStatus.FAILED, OrderStatus.PAID):
            raise ConflictError("این سفارش در وضعیتی نیست که قابل تلاش دوباره باشد.")
        order.status = OrderStatus.PAID
        order.failure_reason = None
        await session.flush()
        await audit.record(
            session,
            "order.retry",
            actor=staff_id,
            user_id=order.user_id,
            entity="order",
            entity_id=order.id,
        )
        return order

    # -- maintenance -------------------------------------------------------
    async def expire_stale(self, session: AsyncSession, *, limit: int = 200) -> list[Order]:
        """Expire unpaid orders past their deadline and refund reserved funds."""
        now = now_utc()
        rows = list(
            (
                await session.execute(
                    select(Order)
                    .where(
                        Order.status.in_((OrderStatus.PENDING_PAYMENT, OrderStatus.AWAITING_REVIEW)),
                        Order.payment_deadline.is_not(None),
                        Order.payment_deadline < now,
                    )
                    .limit(limit)
                )
            ).scalars()
        )
        expired: list[Order] = []
        for order in rows:
            order.status = OrderStatus.EXPIRED
            await session.flush()
            if order.wallet_used_rial > 0:
                user = await session.get(User, order.user_id)
                if user is not None:
                    await user_service.credit(
                        session,
                        user,
                        order.wallet_used_rial,
                        kind=PaymentKind.REFUND,
                        method=PaymentMethod.WALLET,
                        order_id=order.id,
                        description=f"بازگشت وجه سفارش منقضی {order.order_code}",
                    )
            expired.append(order)
        if expired:
            log.info("Expired %d stale orders", len(expired))
        return expired

    # -- admin queries -----------------------------------------------------
    def _filtered(
        self,
        status: OrderStatus | None = None,
        *,
        user_id: int | None = None,
        plan_id: int | None = None,
        panel_id: int | None = None,
        kind: OrderKind | None = None,
        search: str = "",
    ) -> Select:
        stmt = select(Order)
        if status is not None:
            stmt = stmt.where(Order.status == status)
        if user_id is not None:
            stmt = stmt.where(Order.user_id == user_id)
        if plan_id is not None:
            stmt = stmt.where(Order.plan_id == plan_id)
        if panel_id is not None:
            stmt = stmt.where(Order.panel_id == panel_id)
        if kind is not None:
            stmt = stmt.where(Order.kind == kind)
        if search:
            term = search.strip()
            stmt = stmt.where(
                or_(
                    Order.order_code.ilike(f"%{term.upper()}%"),
                    Order.plan_name.ilike(f"%{term}%"),
                    Order.wg_user_id.ilike(f"%{term}%"),
                )
            )
        return stmt

    async def search(
        self,
        session: AsyncSession,
        *,
        status: OrderStatus | None = None,
        limit: int = 30,
        offset: int = 0,
        **filters,
    ) -> tuple[list[Order], int]:
        stmt = self._filtered(status, **filters)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int(await session.scalar(count_stmt) or 0)
        rows = list(
            (await session.execute(stmt.order_by(Order.created_at.desc()).limit(limit).offset(offset))).scalars()
        )
        return rows, total

    async def count_by_status(self, session: AsyncSession, status: OrderStatus) -> int:
        return int(await session.scalar(select(func.count(Order.id)).where(Order.status == status)) or 0)

    async def pending_provisioning(self, session: AsyncSession, *, limit: int = 25) -> list[Order]:
        """Paid orders that still need to reach the node (crash recovery)."""
        return list(
            (
                await session.execute(
                    select(Order)
                    .where(Order.status.in_((OrderStatus.PAID, OrderStatus.PROVISIONING)))
                    .order_by(Order.paid_at.asc())
                    .limit(limit)
                )
            ).scalars()
        )


order_service = OrderService()


def order_snapshot_terms(order: Order) -> dict[str, object]:
    """Terms to apply on the node, taken from the immutable order snapshot."""
    return {
        "traffic_limit_bytes": gb_to_bytes(order.traffic_gb),
        "duration_seconds": days_to_seconds(order.duration_days),
        "device_limit": order.device_limit,
        "speed_limit_down_kbps": order.speed_limit_down_kbps,
        "speed_limit_up_kbps": order.speed_limit_up_kbps,
    }


__all__ = [
    "OPEN_STATUSES",
    "PAID_STATUSES",
    "REVENUE_STATUSES",
    "OrderService",
    "PriceQuote",
    "order_service",
    "order_snapshot_terms",
]
