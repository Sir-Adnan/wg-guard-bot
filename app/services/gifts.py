"""Gift / redeem codes (کد هدیه).

Three flavours, all validated in one place:

* ``wallet``  — credit N Rial to the wallet,
* ``plan``    — grant a free service from a plan in the catalog,
* ``percent`` — bonus percentage applied on top of a wallet top-up.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import format_amount
from app.core.security import random_code
from app.db.models import (
    GiftCode,
    GiftRedemption,
    Order,
    OrderKind,
    PaymentKind,
    PaymentMethod,
    Plan,
    User,
)
from app.services.audit import audit
from app.services.users import user_service

log = get_logger(__name__)

KIND_LABELS: dict[str, str] = {
    "wallet": "شارژ کیف پول",
    "plan": "سرویس رایگان",
    "percent": "درصد جایزه",
}


@dataclass(slots=True)
class GiftResult:
    code: GiftCode
    amount_rial: int = 0
    plan: Plan | None = None
    order_id: int | None = None

    @property
    def label(self) -> str:
        return KIND_LABELS.get(self.code.kind, self.code.kind)


class GiftService:
    """Create, validate and redeem gift codes."""

    # -- lookup ------------------------------------------------------------
    async def find(self, session: AsyncSession, code: str) -> GiftCode | None:
        normalised = code.strip().upper().replace(" ", "")
        if not normalised:
            return None
        return (await session.execute(select(GiftCode).where(GiftCode.code == normalised))).scalar_one_or_none()

    async def validate(self, session: AsyncSession, code: str, user: User) -> GiftCode:
        row = await self.find(session, code)
        if row is None:
            raise ValidationError("کد هدیه وارد شده معتبر نیست.")
        if not row.is_active:
            raise ValidationError("این کد هدیه غیرفعال شده است.")

        now = now_utc()
        if row.starts_at and row.starts_at > now:
            raise ValidationError("این کد هدیه هنوز فعال نشده است.")
        if row.expires_at and row.expires_at < now:
            raise ValidationError("مهلت استفاده از این کد هدیه به پایان رسیده است.")
        if row.max_uses is not None and row.used_count >= row.max_uses:
            raise ValidationError("ظرفیت استفاده از این کد هدیه تکمیل شده است.")

        used = await session.scalar(
            select(func.count(GiftRedemption.id)).where(
                GiftRedemption.code_id == row.id, GiftRedemption.user_id == user.id
            )
        )
        if used is not None and int(used) >= row.per_user_limit:
            raise ValidationError("شما قبلاً از این کد هدیه استفاده کرده‌اید.")
        return row

    async def preview(self, session: AsyncSession, code: str) -> str:
        """Short description of what a code grants (for the confirmation message)."""
        row = await self.find(session, code)
        if row is None:
            return "—"
        if row.kind == "wallet":
            return f"{format_amount(int(row.value))} شارژ کیف پول"
        if row.kind == "percent":
            from app.core.money import fa_digits

            return f"{fa_digits(str(int(row.value)))}٪ جایزه روی شارژ"
        plan = await session.get(Plan, int(row.value)) if row.value else None
        return f"سرویس رایگان «{plan.name}»" if plan else "سرویس رایگان"

    # -- redemption --------------------------------------------------------
    async def redeem(self, session: AsyncSession, code: str, user: User, *, topup_rial: int = 0) -> GiftResult:
        row = await self.validate(session, code, user)
        result = GiftResult(code=row)

        if row.kind == "wallet":
            amount = int(row.value)
            if amount <= 0:
                raise ValidationError("مقدار این کد هدیه نامعتبر است.")
            await user_service.credit(
                session,
                user,
                amount,
                kind=PaymentKind.GIFT,
                method=PaymentMethod.GIFT,
                description=f"کد هدیه {row.code}",
                reference=row.code,
            )
            result.amount_rial = amount

        elif row.kind == "percent":
            if topup_rial <= 0:
                raise ValidationError("این کد فقط هنگام شارژ کیف پول قابل استفاده است.")
            amount = int(topup_rial * min(max(int(row.value), 0), 100) / 100)
            if amount <= 0:
                raise ValidationError("این کد برای این مبلغ جایزه‌ای ایجاد نمی‌کند.")
            await user_service.credit(
                session,
                user,
                amount,
                kind=PaymentKind.GIFT,
                method=PaymentMethod.GIFT,
                description=f"جایزه کد {row.code}",
                reference=row.code,
            )
            result.amount_rial = amount

        elif row.kind == "plan":
            plan = await session.get(Plan, int(row.value)) if row.value else None
            if plan is None or not plan.is_active:
                raise ValidationError("سرویس این کد هدیه در دسترس نیست.")
            if plan.is_test:
                raise ValidationError("این کد به یک پلن تست اشاره می‌کند.")
            result.plan = plan
        else:
            raise ValidationError("نوع این کد هدیه پشتیبانی نمی‌شود.")

        row.used_count = int(row.used_count) + 1
        session.add(GiftRedemption(code_id=row.id, user_id=user.id, amount_rial=result.amount_rial))
        await session.flush()

        await audit.record(
            session,
            "gift.redeem",
            user_id=user.id,
            entity="gift_code",
            entity_id=row.id,
            description=f"{row.code} ({row.kind})",
            meta={"amount_rial": result.amount_rial},
        )
        log.info("Gift code %s redeemed by %s", row.code, user.telegram_id)
        return result

    async def grant_plan_order(self, session: AsyncSession, result: GiftResult, user: User, order: Order) -> None:
        """Link a free-plan order to its redemption record."""
        redemption = (
            (
                await session.execute(
                    select(GiftRedemption)
                    .where(GiftRedemption.code_id == result.code.id, GiftRedemption.user_id == user.id)
                    .order_by(GiftRedemption.id.desc())
                )
            )
            .scalars()
            .first()
        )
        if redemption is not None:
            redemption.order_id = order.id
            result.order_id = order.id
            await session.flush()

    async def create_free_order(self, session: AsyncSession, result: GiftResult, user: User) -> Order:
        """Create a zero-priced order for a ``plan`` gift."""
        from app.services.orders import order_service

        if result.plan is None:
            raise ConflictError("این کد هدیه سرویسی به همراه ندارد.")
        order = await order_service.create(session, user, result.plan, kind=OrderKind.NEW, free=True)
        await self.grant_plan_order(session, result, user, order)
        return order

    # -- admin -------------------------------------------------------------
    async def list_all(self, session: AsyncSession, *, limit: int = 100) -> list[GiftCode]:
        return list(
            (await session.execute(select(GiftCode).order_by(GiftCode.created_at.desc()).limit(limit))).scalars()
        )

    async def create(
        self,
        session: AsyncSession,
        *,
        code: str | None = None,
        kind: str = "wallet",
        value: int = 0,
        max_uses: int | None = None,
        per_user_limit: int = 1,
        expires_at=None,
        note: str | None = None,
    ) -> GiftCode:
        normalised = (code or f"GIFT-{random_code(8)}").strip().upper().replace(" ", "")
        if kind not in KIND_LABELS:
            raise ValidationError("نوع کد هدیه نامعتبر است.")
        if await self.find(session, normalised) is not None:
            raise ValidationError("این کد هدیه قبلاً ساخته شده است.")

        row = GiftCode(
            code=normalised,
            kind=kind,
            value=value,
            max_uses=max_uses,
            per_user_limit=per_user_limit,
            expires_at=expires_at,
            note=note,
            is_active=True,
        )
        session.add(row)
        await session.flush()
        return row

    async def bulk_create(
        self, session: AsyncSession, *, count: int, kind: str = "wallet", value: int = 0, **kwargs
    ) -> list[GiftCode]:
        """Generate a batch of unique codes (for giveaways)."""
        count = min(max(count, 1), 200)
        created: list[GiftCode] = []
        for _ in range(count):
            created.append(await self.create(session, kind=kind, value=value, **kwargs))
        return created

    async def toggle(self, session: AsyncSession, code_id: int, active: bool) -> GiftCode:
        row = await session.get(GiftCode, code_id)
        if row is None:
            raise NotFoundError("کد هدیه پیدا نشد.")
        row.is_active = active
        await session.flush()
        return row

    async def delete(self, session: AsyncSession, code_id: int) -> None:
        row = await session.get(GiftCode, code_id)
        if row is not None:
            await session.delete(row)
            await session.flush()


gifts = GiftService()


__all__ = ["KIND_LABELS", "GiftResult", "GiftService", "gifts"]
