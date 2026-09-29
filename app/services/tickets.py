"""Support tickets (simple two-way thread between a customer and the team)."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.security import random_code
from app.db.models import Staff, Ticket, TicketMessage, TicketSender, TicketStatus, User

log = get_logger(__name__)


class TicketService:
    """Create and advance support threads."""

    # -- lookup ------------------------------------------------------------
    async def get(self, session: AsyncSession, ticket_id: int) -> Ticket:
        ticket = await session.get(Ticket, ticket_id)
        if ticket is None:
            raise NotFoundError("تیکت مورد نظر پیدا نشد.")
        return ticket

    async def get_by_code(self, session: AsyncSession, code: str) -> Ticket | None:
        return (
            await session.execute(select(Ticket).where(Ticket.ticket_code == code.strip().upper()))
        ).scalar_one_or_none()

    async def open_for_user(self, session: AsyncSession, user_id: int) -> Ticket | None:
        """The newest ticket that is still awaiting an answer."""
        return (
            (
                await session.execute(
                    select(Ticket)
                    .where(Ticket.user_id == user_id, Ticket.status != TicketStatus.CLOSED)
                    .order_by(Ticket.last_message_at.desc().nulls_last(), Ticket.id.desc())
                )
            )
            .scalars()
            .first()
        )

    async def list_for_user(self, session: AsyncSession, user_id: int, *, limit: int = 10) -> list[Ticket]:
        return list(
            (
                await session.execute(
                    select(Ticket)
                    .where(Ticket.user_id == user_id)
                    .order_by(Ticket.last_message_at.desc().nulls_last(), Ticket.id.desc())
                    .limit(limit)
                )
            ).scalars()
        )

    async def messages(self, session: AsyncSession, ticket_id: int, *, limit: int = 50) -> list[TicketMessage]:
        return list(
            (
                await session.execute(
                    select(TicketMessage)
                    .where(TicketMessage.ticket_id == ticket_id)
                    .order_by(TicketMessage.created_at.asc())
                    .limit(limit)
                )
            ).scalars()
        )

    # -- creation ----------------------------------------------------------
    async def create(
        self,
        session: AsyncSession,
        user: User,
        *,
        subject: str,
        text: str,
        file_id: str | None = None,
        file_type: str | None = None,
    ) -> Ticket:
        open_tickets = int(
            await session.scalar(
                select(func.count(Ticket.id)).where(Ticket.user_id == user.id, Ticket.status != TicketStatus.CLOSED)
            )
            or 0
        )
        if open_tickets >= 5:
            raise ConflictError("شما چند تیکت باز دارید. ابتدا آن‌ها را ببندید یا منتظر پاسخ بمانید.")

        ticket = Ticket(
            ticket_code=await self._new_code(session),
            user_id=user.id,
            subject=subject[:255],
            status=TicketStatus.OPEN,
            unread_for_staff=1,
            last_message_at=now_utc(),
        )
        session.add(ticket)
        await session.flush()

        session.add(
            TicketMessage(
                ticket_id=ticket.id,
                sender=TicketSender.USER,
                text=text,
                file_id=file_id,
                file_type=file_type,
                delivered=True,
            )
        )
        await session.flush()
        log.info("Ticket %s opened by %s", ticket.ticket_code, user.telegram_id)
        return ticket

    async def _new_code(self, session: AsyncSession) -> str:
        for _ in range(10):
            code = f"T{random_code(5)}"
            if not await session.scalar(select(func.count(Ticket.id)).where(Ticket.ticket_code == code)):
                return code
        from app.core.security import random_token

        return f"T{random_token(6).upper()}"

    # -- messaging ---------------------------------------------------------
    async def add_user_message(
        self,
        session: AsyncSession,
        ticket: Ticket,
        *,
        text: str,
        file_id: str | None = None,
        file_type: str | None = None,
    ) -> TicketMessage:
        if ticket.status == TicketStatus.CLOSED:
            raise ConflictError("این تیکت بسته شده است. لطفاً یک تیکت جدید بسازید.")
        message = TicketMessage(
            ticket_id=ticket.id,
            sender=TicketSender.USER,
            text=text,
            file_id=file_id,
            file_type=file_type,
        )
        session.add(message)
        ticket.status = TicketStatus.OPEN
        ticket.unread_for_staff = int(ticket.unread_for_staff) + 1
        ticket.last_message_at = now_utc()
        await session.flush()
        return message

    async def add_staff_message(
        self, session: AsyncSession, ticket: Ticket, staff: Staff | None, *, text: str
    ) -> TicketMessage:
        message = TicketMessage(
            ticket_id=ticket.id,
            sender=TicketSender.STAFF,
            staff_id=staff.id if staff else None,
            text=text,
        )
        session.add(message)
        ticket.status = TicketStatus.ANSWERED
        ticket.unread_for_user = int(ticket.unread_for_user) + 1
        ticket.unread_for_staff = 0
        if staff is not None and ticket.assigned_staff_id is None:
            ticket.assigned_staff_id = staff.id
        ticket.last_message_at = now_utc()
        await session.flush()
        return message

    async def close(self, session: AsyncSession, ticket: Ticket, *, staff: Staff | None = None) -> Ticket:
        ticket.status = TicketStatus.CLOSED
        ticket.unread_for_staff = 0
        ticket.last_message_at = now_utc()
        await session.flush()
        log.info("Ticket %s closed", ticket.ticket_code)
        return ticket

    async def reopen(self, session: AsyncSession, ticket: Ticket) -> Ticket:
        ticket.status = TicketStatus.OPEN
        ticket.last_message_at = now_utc()
        await session.flush()
        return ticket

    async def mark_read_by_user(self, session: AsyncSession, ticket: Ticket) -> None:
        if ticket.unread_for_user:
            ticket.unread_for_user = 0
            await session.flush()

    # -- admin -------------------------------------------------------------
    async def search(
        self,
        session: AsyncSession,
        *,
        status: TicketStatus | None = None,
        only_unread: bool = False,
        limit: int = 30,
        offset: int = 0,
    ) -> tuple[list[Ticket], int]:
        stmt = select(Ticket)
        if status is not None:
            stmt = stmt.where(Ticket.status == status)
        if only_unread:
            stmt = stmt.where(Ticket.unread_for_staff > 0)

        total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = list(
            (
                await session.execute(
                    stmt.order_by(Ticket.last_message_at.desc().nulls_last(), Ticket.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).scalars()
        )
        return rows, total

    async def open_count(self, session: AsyncSession) -> int:
        return int(await session.scalar(select(func.count(Ticket.id)).where(Ticket.status != TicketStatus.CLOSED)) or 0)

    async def unread_count(self, session: AsyncSession) -> int:
        return int(await session.scalar(select(func.count(Ticket.id)).where(Ticket.unread_for_staff > 0)) or 0)


tickets = TicketService()


__all__ = ["TicketService", "tickets"]
