"""Card-to-card receipt review.

The workflow the shop owner asked for, end to end:

1. the customer transfers money and sends a photo of the receipt,
2. the bot stores it and **broadcasts a copy to every reviewer** (admins and
   support who opted in),
3. any one reviewer approves or rejects,
4. **every other reviewer's copy is edited in place** so nobody acts twice and
   everyone sees who decided what — plus the customer is notified.

Step 4 is why :class:`~app.db.models.ReceiptMessage` exists: it records the
``(chat_id, message_id)`` of each broadcast copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.jalali import jalali_datetime, now_utc
from app.core.logging import get_logger
from app.core.money import format_amount, format_rial
from app.core.security import random_code
from app.db.models import (
    Order,
    OrderStatus,
    PaymentKind,
    PaymentMethod,
    Receipt,
    ReceiptMedia,
    ReceiptMessage,
    ReceiptStatus,
    Staff,
    StaffRole,
    User,
)
from app.services.audit import audit
from app.services.notifications import Media, notifier
from app.services.settings_store import app_settings
from app.services.texts import html_escape, texts
from app.services.users import user_service

log = get_logger(__name__)

PURPOSE_LABELS = {"purchase": "خرید سرویس", "deposit": "شارژ کیف پول"}
REVIEW_ROLES = (StaffRole.OWNER, StaffRole.ADMIN, StaffRole.SUPPORT)


@dataclass(slots=True)
class ReviewOutcome:
    """What happened when a reviewer pressed a button."""

    accepted: bool
    message: str
    receipt_code: str = ""
    already_reviewed_by: str | None = None


class ReceiptService:
    """Create, broadcast and settle payment receipts."""

    # ------------------------------------------------------------------
    # Creation
    # ------------------------------------------------------------------
    async def create(
        self,
        session: AsyncSession,
        user: User,
        *,
        purpose: str,
        amount_rial: int,
        media: ReceiptMedia,
        file_id: str | None = None,
        chat_id: int | None = None,
        message_id: int | None = None,
        note: str | None = None,
        tracking_code: str | None = None,
        payer_card_last4: str | None = None,
        order: Order | None = None,
    ) -> Receipt:
        if amount_rial <= 0:
            raise ValidationError("مبلغ رسید باید بزرگ‌تر از صفر باشد.")
        if purpose not in PURPOSE_LABELS:
            raise ValidationError("نوع رسید نامعتبر است.")

        pending = await self.pending_for_user(session, user.id, purpose=purpose)
        if pending is not None:
            raise ConflictError(f"شما یک رسید در حال بررسی دارید ({pending.code}). تا بررسی آن صبر کنید.")

        receipt = Receipt(
            code=await self._new_code(session),
            user_id=user.id,
            order_id=order.id if order else None,
            purpose=purpose,
            amount_rial=amount_rial,
            media=media,
            file_id=file_id,
            chat_id=chat_id,
            message_id=message_id,
            note=note,
            tracking_code=tracking_code,
            payer_card_last4=payer_card_last4,
            status=ReceiptStatus.PENDING,
        )
        session.add(receipt)
        await session.flush()

        if order is not None and order.status == OrderStatus.PENDING_PAYMENT:
            order.status = OrderStatus.AWAITING_REVIEW
            await session.flush()

        await audit.record(
            session,
            "receipt.create",
            user_id=user.id,
            entity="receipt",
            entity_id=receipt.id,
            description=f"{receipt.code} / {purpose}",
            meta={"amount_rial": amount_rial, "tracking": tracking_code},
        )
        log.info("Receipt %s created for user %s (%s rial)", receipt.code, user.telegram_id, amount_rial)
        return receipt

    async def _new_code(self, session: AsyncSession) -> str:
        for _ in range(12):
            code = random_code(6)
            exists = await session.scalar(select(func.count(Receipt.id)).where(Receipt.code == code))
            if not exists:
                return code
        from app.core.security import random_token

        return random_token(8).upper()

    async def pending_for_user(
        self, session: AsyncSession, user_id: int, *, purpose: str | None = None
    ) -> Receipt | None:
        stmt = select(Receipt).where(Receipt.user_id == user_id, Receipt.status == ReceiptStatus.PENDING)
        if purpose:
            stmt = stmt.where(Receipt.purpose == purpose)
        return (await session.execute(stmt.order_by(Receipt.created_at.desc()))).scalars().first()

    async def get(self, session: AsyncSession, receipt_id: int) -> Receipt:
        receipt = await session.get(Receipt, receipt_id)
        if receipt is None:
            raise NotFoundError("رسید مورد نظر پیدا نشد.")
        return receipt

    # ------------------------------------------------------------------
    # Broadcast to reviewers
    # ------------------------------------------------------------------
    async def broadcast(self, session: AsyncSession, receipt: Receipt) -> list[tuple[int, int]]:
        """Send a copy of the receipt to every reviewer and remember its message id."""
        caption = await self.build_staff_caption(session, receipt)
        media = self._media_of(receipt)
        keyboard = await self._staff_keyboard(session, receipt)

        delivered = await notifier.to_staff(
            session,
            caption,
            roles=REVIEW_ROLES,
            reviewers_only=True,
            media=media,
            keyboard=keyboard,
        )
        for chat_id, message_id in delivered:
            session.add(
                ReceiptMessage(
                    receipt_id=receipt.id,
                    staff_id=await self._staff_id_for(session, chat_id),
                    chat_id=chat_id,
                    message_id=message_id,
                    is_media=media is not None,
                )
            )
        await session.flush()

        if not delivered:
            log.warning("Receipt %s has no reviewer to notify", receipt.code)
            await notifier.record_event(
                "warning",  # type: ignore[arg-type]
                f"رسید {receipt.code} ثبت شد اما هیچ بررسی‌کننده‌ای فعال نبود.",
                source="receipts",
                session=session,
            )
        return delivered

    async def _staff_id_for(self, session: AsyncSession, chat_id: int) -> int | None:
        return await session.scalar(select(Staff.id).where(Staff.telegram_id == chat_id, Staff.is_active.is_(True)))

    @staticmethod
    def _media_of(receipt: Receipt) -> Media | None:
        if not receipt.file_id:
            return None
        if receipt.media == ReceiptMedia.PHOTO:
            return Media(kind="photo", file_id=receipt.file_id)
        if receipt.media == ReceiptMedia.DOCUMENT:
            return Media(kind="document", file_id=receipt.file_id)
        return None

    async def build_staff_caption(self, session: AsyncSession, receipt: Receipt) -> str:
        user = receipt.user or await session.get(User, receipt.user_id)
        order = receipt.order
        base = await texts.get(
            "receipt.admin_caption",
            session,
            code=receipt.code,
            user=user.mention if user else "—",
            amount=format_amount(receipt.amount_rial),
            purpose=PURPOSE_LABELS.get(receipt.purpose, receipt.purpose),
            tracking=receipt.tracking_code or "—",
            card=receipt.payer_card_last4 or "—",
            order=order.order_code if order else "—",
        )
        lines = [await texts.get("receipt.admin_new", session), "", base]
        if receipt.note:
            lines.append(f"\n<b>یادداشت کاربر:</b> {html_escape(receipt.note[:400])}")
        lines.append(f"\n<b>زمان ثبت:</b> {jalali_datetime(receipt.created_at)}")
        return "\n".join(lines)

    async def _staff_keyboard(self, session: AsyncSession, receipt: Receipt):
        from app.bot.callbacks import ReceiptCB
        from app.bot.keyboards import KeyboardBuilder

        kb = KeyboardBuilder(session=session, columns=2)
        await kb.add("receipt.approve", callback=ReceiptCB(action="approve", receipt_id=receipt.id).pack())
        await kb.add("receipt.reject", callback=ReceiptCB(action="reject", receipt_id=receipt.id).pack())
        await kb.add("receipt.view_user", callback=ReceiptCB(action="view_user", receipt_id=receipt.id).pack())
        return kb.build()

    # ------------------------------------------------------------------
    # Review
    # ------------------------------------------------------------------
    async def approve(
        self,
        session: AsyncSession,
        receipt: Receipt,
        staff: Staff | None,
        *,
        amount_rial: int | None = None,
        note: str | None = None,
    ) -> ReviewOutcome:
        if receipt.status == ReceiptStatus.APPROVED:
            return ReviewOutcome(
                accepted=False,
                message="این رسید قبلاً تأیید شده است.",
                receipt_code=receipt.code,
                already_reviewed_by=await self._reviewer_name(session, receipt),
            )
        if receipt.status == ReceiptStatus.REJECTED:
            return ReviewOutcome(
                accepted=False,
                message="این رسید قبلاً رد شده است.",
                receipt_code=receipt.code,
                already_reviewed_by=await self._reviewer_name(session, receipt),
            )

        approved_amount = int(amount_rial if amount_rial is not None else receipt.amount_rial)
        receipt.status = ReceiptStatus.APPROVED
        receipt.reviewed_by_staff_id = staff.id if staff else None
        receipt.reviewed_at = now_utc()
        receipt.amount_rial = approved_amount
        await session.flush()

        user = receipt.user or await session.get(User, receipt.user_id)
        order = receipt.order

        # Money movement happens here and nowhere else for card payments.
        if receipt.purpose == "deposit":
            if user is not None:
                await user_service.credit(
                    session,
                    user,
                    approved_amount,
                    kind=PaymentKind.DEPOSIT,
                    method=PaymentMethod.CARD,
                    description=f"شارژ کیف پول — رسید {receipt.code}",
                    reference=receipt.code,
                    staff_id=staff.id if staff else None,
                )
        elif order is not None:
            from app.services.orders import order_service

            await order_service.mark_paid(
                session,
                order,
                method=PaymentMethod.CARD,
                reference=receipt.code,
                staff_id=staff.id if staff else None,
            )

        await audit.record(
            session,
            "receipt.approve",
            actor=staff,
            user_id=receipt.user_id,
            entity="receipt",
            entity_id=receipt.id,
            description=f"{receipt.code} approved",
            meta={"amount_rial": approved_amount, "note": note},
        )
        return ReviewOutcome(accepted=True, message="رسید تأیید شد.", receipt_code=receipt.code)

    async def reject(
        self,
        session: AsyncSession,
        receipt: Receipt,
        staff: Staff | None,
        *,
        reason: str,
    ) -> ReviewOutcome:
        if receipt.status != ReceiptStatus.PENDING:
            return ReviewOutcome(
                accepted=False,
                message="این رسید قبلاً بررسی شده است.",
                receipt_code=receipt.code,
                already_reviewed_by=await self._reviewer_name(session, receipt),
            )

        receipt.status = ReceiptStatus.REJECTED
        receipt.reviewed_by_staff_id = staff.id if staff else None
        receipt.reviewed_at = now_utc()
        receipt.reject_reason = (reason or "بدون توضیح")[:255]
        await session.flush()

        order = receipt.order
        if order is not None and order.status in (OrderStatus.AWAITING_REVIEW, OrderStatus.PENDING_PAYMENT):
            # Give the customer another chance to send a correct receipt.
            order.status = OrderStatus.PENDING_PAYMENT
            order.payment_deadline = now_utc() + timedelta(
                minutes=app_settings.get_int("shop.receipt_expire_minutes", 90)
            )
            await session.flush()

        await audit.record(
            session,
            "receipt.reject",
            actor=staff,
            user_id=receipt.user_id,
            entity="receipt",
            entity_id=receipt.id,
            description=f"{receipt.code} rejected: {reason}",
        )
        return ReviewOutcome(accepted=True, message="رسید رد شد.", receipt_code=receipt.code)

    async def _reviewer_name(self, session: AsyncSession, receipt: Receipt) -> str | None:
        if receipt.reviewed_by_staff_id is None:
            return None
        staff = await session.get(Staff, receipt.reviewed_by_staff_id)
        if staff is None:
            return None
        return staff.name or (f"@{staff.login}" if staff.login else f"#{staff.id}")

    # ------------------------------------------------------------------
    # Fan-out of the decision
    # ------------------------------------------------------------------
    async def refresh_copies(
        self,
        session: AsyncSession,
        receipt: Receipt,
        *,
        reviewer: Staff | None,
        decision: str,
        reason: str | None = None,
    ) -> int:
        """Edit every reviewer copy so the whole team sees the same status."""
        if receipt.status == ReceiptStatus.APPROVED:
            suffix = await texts.get(
                "receipt.admin_approved_by",
                session,
                admin=reviewer.name if reviewer and reviewer.name else "مدیر",
            )
        elif receipt.status == ReceiptStatus.REJECTED:
            suffix = await texts.get(
                "receipt.admin_rejected_by",
                session,
                admin=reviewer.name if reviewer and reviewer.name else "مدیر",
                reason=receipt.reject_reason or "—",
            )
        else:  # back to pending
            suffix = "⏳ در انتظار بررسی"

        header = await texts.get("receipt.admin_new", session)
        caption = await self.build_staff_caption(session, receipt)
        final_text = f"{header}\n\n{caption}\n\n{suffix}"

        media = self._media_of(receipt)
        edited = 0
        for copy in list(receipt.messages):
            ok = False
            if copy.is_media and media is not None:
                ok = await notifier.edit_caption(copy.chat_id, copy.message_id, final_text, clear_keyboard=True)
            if not ok:
                ok = await notifier.edit(copy.chat_id, copy.message_id, final_text)
            if ok:
                copy.edited = True
                edited += 1
        await session.flush()
        return edited

    async def notify_customer(
        self,
        session: AsyncSession,
        receipt: Receipt,
        *,
        approved: bool,
    ) -> None:
        user = receipt.user or await session.get(User, receipt.user_id)
        if user is None:
            return
        if approved:
            body = await texts.get(
                "receipt.approved", session, code=receipt.code, amount=format_amount(receipt.amount_rial)
            )
        else:
            body = await texts.get(
                "receipt.rejected",
                session,
                code=receipt.code,
                reason=html_escape(receipt.reject_reason or "—"),
            )
        await notifier.to_user(user, body)

    # ------------------------------------------------------------------
    # Queries for the panel
    # ------------------------------------------------------------------
    async def search(
        self,
        session: AsyncSession,
        *,
        status: ReceiptStatus | None = None,
        user_id: int | None = None,
        query: str = "",
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[Receipt], int]:
        stmt = select(Receipt)
        if status is not None:
            stmt = stmt.where(Receipt.status == status)
        if user_id is not None:
            stmt = stmt.where(Receipt.user_id == user_id)
        if query:
            term = query.strip().lstrip("#")
            stmt = stmt.where(Receipt.code.ilike(f"%{term}%") | Receipt.tracking_code.ilike(f"%{term}%"))

        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = int(await session.scalar(count_stmt) or 0)
        rows = list(
            (await session.execute(stmt.order_by(Receipt.created_at.desc()).limit(limit).offset(offset))).scalars()
        )
        return rows, total

    async def pending_count(self, session: AsyncSession) -> int:
        return int(
            await session.scalar(select(func.count(Receipt.id)).where(Receipt.status == ReceiptStatus.PENDING)) or 0
        )

    async def expire_stale(self, session: AsyncSession, *, limit: int = 100) -> list[Receipt]:
        """Close receipts whose payment window has elapsed."""
        cutoff = now_utc() - timedelta(minutes=app_settings.get_int("shop.receipt_expire_minutes", 90) * 4)
        rows = list(
            (
                await session.execute(
                    select(Receipt)
                    .where(Receipt.status == ReceiptStatus.PENDING, Receipt.created_at < cutoff)
                    .limit(limit)
                )
            ).scalars()
        )
        for receipt in rows:
            receipt.status = ReceiptStatus.EXPIRED
        if rows:
            await session.flush()
            log.info("Expired %d stale receipts", len(rows))
        return rows

    # ------------------------------------------------------------------
    # Summary text for a receipt (used by /receipt admin commands)
    # ------------------------------------------------------------------
    async def summary(self, session: AsyncSession, receipt: Receipt) -> str:
        user = receipt.user or await session.get(User, receipt.user_id)
        return (
            f"<b>رسید {receipt.code}</b>\n"
            f"کاربر: {user.mention if user else '—'}\n"
            f"مبلغ: {format_rial(receipt.amount_rial)}\n"
            f"وضعیت: {self.status_label(receipt.status)}\n"
            f"زمان: {jalali_datetime(receipt.created_at)}"
        )

    @staticmethod
    def status_label(status: ReceiptStatus) -> str:
        return {
            ReceiptStatus.PENDING: "⏳ در انتظار بررسی",
            ReceiptStatus.APPROVED: "✅ تأییدشده",
            ReceiptStatus.REJECTED: "❌ ردشده",
            ReceiptStatus.EXPIRED: "⌛ منقضی",
        }.get(status, status.value)


receipt_service = ReceiptService()


__all__ = ["PURPOSE_LABELS", "REVIEW_ROLES", "ReceiptService", "ReviewOutcome", "receipt_service"]
