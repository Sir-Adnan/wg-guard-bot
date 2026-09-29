"""Broadcast (پیام همگانی) with throttling and resumable progress.

Telegram rate-limits bulk sending, so the sender walks the audience with a
configurable delay, records progress on every batch and keeps going after a
transient failure.  The task registry lets the panel cancel a running campaign.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.db.models import (
    Broadcast,
    BroadcastStatus,
    Service,
    ServiceStatus,
    Staff,
    User,
)
from app.db.session import session_scope
from app.services.audit import audit
from app.services.notifications import Media, notifier

log = get_logger(__name__)

AUDIENCES: dict[str, str] = {
    "all": "همه کاربران",
    "active": "کاربران دارای سرویس فعال",
    "expired": "کاربران با سرویس منقضی",
    "no_service": "کاربران بدون سرویس",
    "with_balance": "کاربران دارای موجودی",
}

#: Telegram tolerates ~30 messages/second overall; stay well under it.
DEFAULT_DELAY = 0.06
PROGRESS_EVERY = 25


@dataclass(slots=True)
class BroadcastProgress:
    broadcast_id: int
    total: int = 0
    sent: int = 0
    failed: int = 0
    done: bool = False


class BroadcastService:
    """Creates and executes broadcast campaigns."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def is_running(self, broadcast_id: int) -> bool:
        task = self._tasks.get(broadcast_id)
        return task is not None and not task.done()

    # -- audience ----------------------------------------------------------
    async def audience_ids(self, session: AsyncSession, audience: str) -> list[int]:
        if audience not in AUDIENCES:
            raise ValidationError("گروه مخاطبان نامعتبر است.")

        stmt = select(User.telegram_id).where(User.is_blocked.is_(False), User.is_bot_blocked.is_(False))

        if audience == "all":
            pass
        elif audience == "with_balance":
            stmt = stmt.where(User.balance_rial > 0)
        else:
            active_users = select(Service.user_id).where(Service.status == ServiceStatus.ACTIVE)
            expired_users = select(Service.user_id).where(
                Service.status.in_((ServiceStatus.EXPIRED, ServiceStatus.TRAFFIC_EXCEEDED))
            )
            if audience == "active":
                stmt = stmt.where(User.id.in_(active_users))
            elif audience == "expired":
                stmt = stmt.where(User.id.in_(expired_users), User.id.not_in(active_users))
            elif audience == "no_service":
                any_service = select(Service.user_id)
                stmt = stmt.where(User.id.not_in(any_service))
        return [int(row) for row in (await session.execute(stmt)).scalars()]

    async def preview_count(self, session: AsyncSession, audience: str) -> int:
        return len(await self.audience_ids(session, audience))

    # -- creation ----------------------------------------------------------
    async def create(
        self,
        session: AsyncSession,
        staff: Staff | None,
        *,
        text: str,
        audience: str = "all",
        media: Media | None = None,
        buttons: list[dict] | None = None,
        start_immediately: bool = True,
    ) -> Broadcast:
        if not text.strip():
            raise ValidationError("متن پیام نمی‌تواند خالی باشد.")
        if audience not in AUDIENCES:
            raise ValidationError("گروه مخاطبان نامعتبر است.")

        broadcast = Broadcast(
            created_by_staff_id=staff.id if staff else None,
            text=text,
            audience=audience,
            media_file_id=media.file_id if media else None,
            media_type=media.kind if media else None,
            buttons=buttons or None,
            status=BroadcastStatus.DRAFT,
            total=await self.preview_count(session, audience),
        )
        session.add(broadcast)
        await session.flush()

        await audit.record(
            session,
            "broadcast.create",
            actor=staff,
            entity="broadcast",
            entity_id=broadcast.id,
            description=f"audience={audience} total={broadcast.total}",
        )
        if start_immediately:
            await session.flush()
        return broadcast

    # -- execution ---------------------------------------------------------
    async def start(self, broadcast_id: int) -> bool:
        """Kick off (or resume) a campaign in the background."""
        if not notifier.bound:
            raise ValidationError("ربات تلگرام فعال نیست، بنابراین ارسال همگانی ممکن نیست. BOT_TOKEN را بررسی کنید.")
        if self.is_running(broadcast_id):
            return False
        task = asyncio.create_task(self._run(broadcast_id), name=f"broadcast-{broadcast_id}")
        self._tasks[broadcast_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(broadcast_id, None))
        return True

    async def cancel(self, broadcast_id: int) -> bool:
        task = self._tasks.get(broadcast_id)
        if task is not None and not task.done():
            task.cancel()
        async with session_scope() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return False
            if broadcast.status == BroadcastStatus.RUNNING:
                broadcast.status = BroadcastStatus.CANCELED
                broadcast.finished_at = now_utc()
        return True

    async def _run(self, broadcast_id: int) -> None:
        async with session_scope() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return
            if broadcast.status == BroadcastStatus.DONE:
                return
            if broadcast.status == BroadcastStatus.RUNNING:
                raise ConflictError("این ارسال همگانی در حال اجراست.")
            broadcast.status = BroadcastStatus.RUNNING
            broadcast.started_at = broadcast.started_at or now_utc()
            text = broadcast.text
            media = (
                Media(kind=broadcast.media_type, file_id=broadcast.media_file_id)  # type: ignore[arg-type]
                if broadcast.media_type and broadcast.media_file_id
                else None
            )
            audience = broadcast.audience
            targets = await self.audience_ids(session, audience)
            broadcast.total = len(targets)

        sent = 0
        failed = 0
        try:
            for index, telegram_id in enumerate(targets, start=1):
                message = await notifier.send(telegram_id, text, media=media)
                if message is None:
                    failed += 1
                else:
                    sent += 1
                if index % PROGRESS_EVERY == 0:
                    await self._save_progress(broadcast_id, sent, failed)
                await asyncio.sleep(DEFAULT_DELAY)
        except asyncio.CancelledError:
            await self._save_progress(broadcast_id, sent, failed, status=BroadcastStatus.CANCELED)
            log.info("Broadcast %s canceled after %d sends", broadcast_id, sent)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Broadcast %s crashed", broadcast_id)
            await self._save_progress(broadcast_id, sent, failed, status=BroadcastStatus.FAILED)
            await notifier.report_error(exc, source="broadcast")
            return

        await self._save_progress(broadcast_id, sent, failed, status=BroadcastStatus.DONE)
        log.info("Broadcast %s finished: %d sent / %d failed", broadcast_id, sent, failed)

    async def _save_progress(
        self,
        broadcast_id: int,
        sent: int,
        failed: int,
        *,
        status: BroadcastStatus | None = None,
    ) -> None:
        async with session_scope() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return
            broadcast.sent = sent
            broadcast.failed = failed
            if status is not None:
                broadcast.status = status
                broadcast.finished_at = (
                    now_utc()
                    if status
                    in (
                        BroadcastStatus.DONE,
                        BroadcastStatus.CANCELED,
                        BroadcastStatus.FAILED,
                    )
                    else None
                )

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    # -- queries -----------------------------------------------------------
    async def list_recent(self, session: AsyncSession, *, limit: int = 25) -> list[Broadcast]:
        return list(
            (await session.execute(select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit))).scalars()
        )

    async def get(self, session: AsyncSession, broadcast_id: int) -> Broadcast:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None:
            raise NotFoundError("ارسال همگانی مورد نظر پیدا نشد.")
        return broadcast


broadcasts = BroadcastService()


__all__ = ["AUDIENCES", "BroadcastProgress", "BroadcastService", "broadcasts"]
