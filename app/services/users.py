"""Customer records and the wallet ledger.

The wallet is the single place where money moves.  ``credit`` / ``debit`` are
the only sanctioned mutations of ``users.balance_rial`` and both write a
:class:`~app.db.models.Payment` row in the same transaction, so the ledger and
the balance can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from aiogram.types import User as TgUser
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import InsufficientFunds, NotFoundError, ValidationError
from app.core.jalali import now_utc
from app.core.locales import default_text
from app.core.logging import get_logger
from app.core.security import random_code
from app.db.models import (
    Order,
    OrderStatus,
    Payment,
    PaymentKind,
    PaymentMethod,
    Service,
    ServiceStatus,
    User,
)

log = get_logger(__name__)


@dataclass(slots=True)
class UserStats:
    orders_total: int = 0
    orders_paid: int = 0
    services_active: int = 0
    spent_rial: int = 0
    referrals: int = 0


class UserService:
    """Everything that reads or writes a customer row."""

    # -- lookup ------------------------------------------------------------
    async def get_by_telegram_id(self, session: AsyncSession, telegram_id: int) -> User | None:
        return (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()

    async def get(self, session: AsyncSession, user_id: int) -> User:
        user = await session.get(User, user_id)
        if user is None:
            raise NotFoundError("کاربر مورد نظر پیدا نشد.")
        return user

    async def get_or_create(
        self, session: AsyncSession, tg_user: TgUser, *, referrer_code: str | None = None
    ) -> tuple[User, bool]:
        """Fetch the customer, creating the row on first contact."""
        user = await self.get_by_telegram_id(session, tg_user.id)
        created = False

        if user is None:
            user = User(
                telegram_id=tg_user.id,
                username=tg_user.username,
                first_name=tg_user.first_name,
                last_name=tg_user.last_name,
                language_code=tg_user.language_code,
                referral_code=self._new_referral_code(),
                last_seen_at=now_utc(),
            )
            session.add(user)
            await session.flush()
            created = True
            log.info("New customer %s (@%s)", tg_user.id, tg_user.username or "-")
        else:
            self._sync_profile(user, tg_user)

        if created and referrer_code:
            await self._apply_referrer(session, user, referrer_code)

        return user, created

    def _sync_profile(self, user: User, tg_user: TgUser) -> None:
        """Keep the cached Telegram profile fields fresh."""
        user.username = tg_user.username
        user.first_name = tg_user.first_name
        user.last_name = tg_user.last_name
        if tg_user.language_code:
            user.language_code = tg_user.language_code
        user.last_seen_at = now_utc()

    async def touch(self, session: AsyncSession, user: User, tg_user: TgUser | None = None) -> None:
        if tg_user is not None:
            self._sync_profile(user, tg_user)
        else:
            user.last_seen_at = now_utc()
        await session.flush()

    @staticmethod
    def _new_referral_code() -> str:
        return f"wg{random_code(6)}"

    async def apply_referral(self, session: AsyncSession, user: User, code: str) -> bool:
        """Attribute ``user`` to the owner of ``code`` (once)."""
        if not code or user.referred_by_id is not None or code == user.referral_code:
            return False
        before = user.referred_by_id
        await self._apply_referrer(session, user, code)
        return user.referred_by_id != before

    async def _apply_referrer(self, session: AsyncSession, user: User, code: str) -> None:
        if not code or code == user.referral_code:
            return
        referrer = (await session.execute(select(User).where(User.referral_code == code))).scalar_one_or_none()
        if referrer is None or referrer.id == user.id:
            return
        user.referred_by_id = referrer.id
        await session.flush()
        log.info("Customer %s attributed to referrer %s", user.telegram_id, referrer.telegram_id)

    # -- state -------------------------------------------------------------
    async def set_blocked(self, session: AsyncSession, user: User, blocked: bool, reason: str | None = None) -> None:
        user.is_blocked = blocked
        user.block_reason = reason if blocked else None
        await session.flush()

    # -- wallet ------------------------------------------------------------
    async def credit(
        self,
        session: AsyncSession,
        user: User,
        amount_rial: int,
        *,
        kind: PaymentKind = PaymentKind.DEPOSIT,
        method: PaymentMethod | None = None,
        order_id: int | None = None,
        description: str | None = None,
        reference: str | None = None,
        staff_id: int | None = None,
    ) -> Payment:
        """Add funds and record the ledger entry."""
        if amount_rial <= 0:
            raise ValidationError("مبلغ باید بزرگ‌تر از صفر باشد.")
        await self._lock_wallet(session, user)
        user.balance_rial = int(user.balance_rial) + int(amount_rial)
        entry = Payment(
            user_id=user.id,
            order_id=order_id,
            kind=kind,
            method=method,
            amount_rial=int(amount_rial),
            balance_after_rial=user.balance_rial,
            reference=reference,
            description=description,
            created_by_staff_id=staff_id,
        )
        session.add(entry)
        await session.flush()
        return entry

    async def debit(
        self,
        session: AsyncSession,
        user: User,
        amount_rial: int,
        *,
        kind: PaymentKind = PaymentKind.PURCHASE,
        method: PaymentMethod | None = PaymentMethod.WALLET,
        order_id: int | None = None,
        description: str | None = None,
        reference: str | None = None,
        staff_id: int | None = None,
        allow_partial: bool = False,
    ) -> Payment:
        """Remove funds; raises :class:`InsufficientFunds` unless partial is allowed."""
        if amount_rial <= 0:
            raise ValidationError("مبلغ باید بزرگ‌تر از صفر باشد.")
        await self._lock_wallet(session, user)
        if not allow_partial and user.balance_rial < amount_rial:
            raise InsufficientFunds(default_text("error.wallet_balance"))
        charged = min(int(amount_rial), int(user.balance_rial)) if allow_partial else int(amount_rial)
        user.balance_rial = int(user.balance_rial) - charged
        entry = Payment(
            user_id=user.id,
            order_id=order_id,
            kind=kind,
            method=method,
            amount_rial=-charged,
            balance_after_rial=user.balance_rial,
            reference=reference,
            description=description,
            created_by_staff_id=staff_id,
        )
        session.add(entry)
        await session.flush()
        return entry

    async def set_balance(
        self,
        session: AsyncSession,
        user: User,
        new_balance_rial: int,
        *,
        description: str,
        staff_id: int | None = None,
    ) -> Payment:
        """Force the balance to an absolute value (admin correction)."""
        await self._lock_wallet(session, user)
        delta = int(new_balance_rial) - int(user.balance_rial)
        if delta == 0:
            raise ValidationError("موجودی تغییری نکرده است.")
        if delta > 0:
            return await self.credit(
                session,
                user,
                delta,
                kind=PaymentKind.ADJUSTMENT,
                description=description,
                staff_id=staff_id,
            )
        return await self.debit(
            session,
            user,
            -delta,
            kind=PaymentKind.ADJUSTMENT,
            method=None,
            description=description,
            staff_id=staff_id,
        )

    @staticmethod
    async def _lock_wallet(session: AsyncSession, user: User) -> None:
        """Re-read the balance under a row lock before changing the ledger."""
        await session.flush()
        await session.scalar(
            select(User).where(User.id == user.id).with_for_update(of=User).execution_options(populate_existing=True)
        )

    async def history(self, session: AsyncSession, user_id: int, *, limit: int = 20, offset: int = 0) -> list[Payment]:
        return list(
            (
                await session.execute(
                    select(Payment)
                    .where(Payment.user_id == user_id)
                    .order_by(Payment.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
        )

    # -- stats -------------------------------------------------------------
    async def stats(self, session: AsyncSession, user: User) -> UserStats:
        paid_statuses = (OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.COMPLETED)

        total = await session.scalar(select(func.count(Order.id)).where(Order.user_id == user.id)) or 0
        paid = (
            await session.scalar(
                select(func.count(Order.id)).where(Order.user_id == user.id, Order.status.in_(paid_statuses))
            )
            or 0
        )
        active = (
            await session.scalar(
                select(func.count(Service.id)).where(Service.user_id == user.id, Service.status == ServiceStatus.ACTIVE)
            )
            or 0
        )
        spent = (
            await session.scalar(
                select(func.coalesce(func.sum(Payment.amount_rial), 0)).where(
                    Payment.user_id == user.id,
                    Payment.kind == PaymentKind.PURCHASE,
                )
            )
            or 0
        )
        referrals = await session.scalar(select(func.count(User.id)).where(User.referred_by_id == user.id)) or 0
        return UserStats(
            orders_total=int(total),
            orders_paid=int(paid),
            services_active=int(active),
            spent_rial=abs(int(spent)),
            referrals=int(referrals),
        )

    # -- admin listings ----------------------------------------------------
    async def search(
        self,
        session: AsyncSession,
        query: str = "",
        *,
        limit: int = 25,
        offset: int = 0,
        blocked: bool | None = None,
    ) -> tuple[list[User], int]:
        stmt = select(User)
        count_stmt = select(func.count(User.id))

        if query:
            term = query.strip()
            conditions = [
                User.username.ilike(f"%{term.lstrip('@')}%"),
                User.first_name.ilike(f"%{term}%"),
                User.last_name.ilike(f"%{term}%"),
            ]
            if term.isdigit():
                conditions.append(User.telegram_id == int(term))
            stmt = stmt.where(or_(*conditions))
            count_stmt = count_stmt.where(or_(*conditions))
        if blocked is not None:
            stmt = stmt.where(User.is_blocked.is_(blocked))
            count_stmt = count_stmt.where(User.is_blocked.is_(blocked))

        total = int(await session.scalar(count_stmt) or 0)
        rows = list(
            (await session.execute(stmt.order_by(User.created_at.desc()).limit(limit).offset(offset))).scalars()
        )
        return rows, total

    async def count(self, session: AsyncSession, *, since: datetime | None = None) -> int:
        stmt = select(func.count(User.id))
        if since is not None:
            stmt = stmt.where(User.created_at >= since)
        return int(await session.scalar(stmt) or 0)

    async def all_telegram_ids(self, session: AsyncSession, *, only_active: bool = True) -> list[int]:
        stmt = select(User.telegram_id)
        if only_active:
            stmt = stmt.where(User.is_blocked.is_(False), User.is_bot_blocked.is_(False))
        return [int(row) for row in (await session.execute(stmt)).scalars()]


user_service = UserService()


__all__ = ["UserService", "UserStats", "user_service"]
