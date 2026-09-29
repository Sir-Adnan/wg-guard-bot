"""Discount codes.

Pure computation plus redemption bookkeeping — no Telegram, no HTTP, so the
rules are trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import format_amount
from app.db.models import DiscountCode, DiscountKind, DiscountRedemption, PaymentMethod, User

log = get_logger(__name__)


@dataclass(slots=True)
class DiscountResult:
    code: DiscountCode
    amount_rial: int

    @property
    def label(self) -> str:
        return f"{self.code.code} ({format_amount(self.amount_rial)})"


class DiscountService:
    """Validates a code against a user/plan and computes the reduction."""

    async def find(self, session: AsyncSession, code: str) -> DiscountCode | None:
        normalised = code.strip().upper()
        if not normalised:
            return None
        return (await session.execute(select(DiscountCode).where(DiscountCode.code == normalised))).scalar_one_or_none()

    async def validate(
        self,
        session: AsyncSession,
        code: str,
        *,
        user: User,
        amount_rial: int,
        plan_id: int | None,
    ) -> DiscountCode:
        """Return the code or raise :class:`ValidationError` with a Persian reason."""
        row = await self.find(session, code)
        if row is None:
            raise ValidationError("کد تخفیف وارد شده معتبر نیست.")
        if not row.is_active:
            raise ValidationError("این کد تخفیف غیرفعال شده است.")

        now = now_utc()
        if row.starts_at and row.starts_at > now:
            raise ValidationError("این کد تخفیف هنوز فعال نشده است.")
        if row.expires_at and row.expires_at < now:
            raise ValidationError("مهلت استفاده از این کد تخفیف به پایان رسیده است.")
        if row.max_uses is not None and row.used_count >= row.max_uses:
            raise ValidationError("ظرفیت استفاده از این کد تخفیف تکمیل شده است.")
        if amount_rial < row.min_amount_rial:
            raise ValidationError(f"این کد برای خرید‌های بالای {format_amount(row.min_amount_rial)} قابل استفاده است.")

        per_user = await session.scalar(
            select(func.count(DiscountRedemption.id)).where(
                DiscountRedemption.code_id == row.id, DiscountRedemption.user_id == user.id
            )
        )
        if per_user is not None and int(per_user) >= row.per_user_limit:
            raise ValidationError("شما قبلاً از این کد تخفیف استفاده کرده‌اید.")

        if row.applies_to_plans and plan_id is not None and plan_id not in row.applies_to_plans:
            raise ValidationError("این کد تخفیف برای پلن انتخابی معتبر نیست.")

        return row

    @staticmethod
    def compute(row: DiscountCode, amount_rial: int) -> int:
        """Discount amount in Rial, never exceeding the base amount."""
        if row.kind == DiscountKind.PERCENT:
            value = round(amount_rial * min(max(row.value, 0), 100) / 100)
        else:
            value = int(row.value)
        if row.max_discount_rial:
            value = min(value, int(row.max_discount_rial))
        return max(0, min(value, amount_rial))

    async def apply(
        self,
        session: AsyncSession,
        code: str,
        *,
        user: User,
        amount_rial: int,
        plan_id: int | None,
        order_id: int | None = None,
    ) -> DiscountResult:
        row = await self.validate(session, code, user=user, amount_rial=amount_rial, plan_id=plan_id)
        value = self.compute(row, amount_rial)
        if value <= 0:
            raise ValidationError("این کد تخفیفی برای این خرید ایجاد نمی‌کند.")

        row.used_count = int(row.used_count) + 1
        session.add(DiscountRedemption(code_id=row.id, user_id=user.id, order_id=order_id, amount_rial=value))
        await session.flush()
        log.info("Discount %s applied for user %s: %s", row.code, user.telegram_id, value)
        return DiscountResult(code=row, amount_rial=value)

    # -- admin -------------------------------------------------------------
    async def list_all(self, session: AsyncSession, *, limit: int = 100) -> list[DiscountCode]:
        return list(
            (
                await session.execute(select(DiscountCode).order_by(DiscountCode.created_at.desc()).limit(limit))
            ).scalars()
        )

    async def create(
        self,
        session: AsyncSession,
        *,
        code: str,
        kind: DiscountKind,
        value: int,
        max_uses: int | None = None,
        per_user_limit: int = 1,
        min_amount_rial: int = 0,
        max_discount_rial: int | None = None,
        expires_at=None,
        applies_to_plans: list[int] | None = None,
        note: str | None = None,
    ) -> DiscountCode:
        normalised = code.strip().upper()
        if not normalised:
            raise ValidationError("کد تخفیف نمی‌تواند خالی باشد.")
        if await self.find(session, normalised) is not None:
            raise ValidationError("این کد تخفیف قبلاً ساخته شده است.")

        row = DiscountCode(
            code=normalised,
            kind=kind,
            value=value,
            max_uses=max_uses,
            per_user_limit=per_user_limit,
            min_amount_rial=min_amount_rial,
            max_discount_rial=max_discount_rial,
            expires_at=expires_at,
            applies_to_plans=applies_to_plans,
            note=note,
            is_active=True,
        )
        session.add(row)
        await session.flush()
        return row

    async def toggle(self, session: AsyncSession, code_id: int, active: bool) -> DiscountCode:
        row = await session.get(DiscountCode, code_id)
        if row is None:
            raise ValidationError("کد تخفیف پیدا نشد.")
        row.is_active = active
        await session.flush()
        return row

    async def delete(self, session: AsyncSession, code_id: int) -> None:
        row = await session.get(DiscountCode, code_id)
        if row is not None:
            await session.delete(row)
            await session.flush()


discounts = DiscountService()


def default_payment_methods(*, card: bool, wallet: bool) -> list[PaymentMethod]:
    """Available payment methods given the current shop configuration."""
    methods: list[PaymentMethod] = []
    if wallet:
        methods.append(PaymentMethod.WALLET)
    if card:
        methods.append(PaymentMethod.CARD)
    return methods


__all__ = ["DiscountResult", "DiscountService", "default_payment_methods", "discounts"]
