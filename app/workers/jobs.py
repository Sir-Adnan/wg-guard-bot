"""Periodic jobs.

Each job is small, idempotent and safe to run twice — the scheduler may fire one
while the previous run is still going after a slow network call.  Jobs open their
own session via :func:`~app.db.session.session_scope` so a failure in one never
poisons another.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import timedelta
from pathlib import Path

from sqlalchemy import delete, select

from app.core.cache import cache
from app.core.config import BASE_DIR, settings
from app.core.jalali import humanize_delta, jalali_date, now_utc
from app.core.logging import get_logger
from app.core.money import format_bytes
from app.db.models import (
    EventLevel,
    Order,
    Service,
    ServiceStatus,
    SystemEvent,
)
from app.db.session import session_scope
from app.panels.manager import panel_manager
from app.services.notifications import notifier
from app.services.orders import order_service
from app.services.provisioning import provisioning
from app.services.receipts import receipt_service
from app.services.settings_store import app_settings
from app.services.texts import texts

log = get_logger(__name__)

BACKUP_DIR = BASE_DIR / "backups"


class Jobs:
    """Container for every scheduled unit of work."""

    def __init__(self) -> None:
        self._provision_lock = asyncio.Lock()

    # -- housekeeping ------------------------------------------------------
    async def expire_payments(self) -> None:
        """Close orders and receipts whose payment window has elapsed."""
        async with session_scope() as session:
            orders = await order_service.expire_stale(session)
            receipts = await receipt_service.expire_stale(session)

        for order in orders:
            try:
                async with session_scope() as session:
                    user = order.user
                    if user is None:
                        continue
                    body = await texts.get(
                        "buy.payment_timeout",
                        session,
                        order=order.order_code,
                        minutes=str(app_settings.get_int("shop.receipt_expire_minutes", 90)),
                    )
                    await notifier.to_user(user, body)
            except Exception as exc:  # pragma: no cover
                log.debug("expiry notice failed: %s", exc)

        if orders or receipts:
            log.info("Housekeeping: %d orders / %d receipts expired", len(orders), len(receipts))

    async def panel_health(self) -> None:
        async with session_scope() as session:
            results = await panel_manager.check_all(session)
        offline = [pid for pid, health in results.items() if health.value == "offline"]
        if offline:
            async with session_scope() as session:
                await notifier.record_event(
                    EventLevel.WARNING,
                    f"{len(offline)} پنل در دسترس نیست (شناسه: {', '.join(map(str, offline))}).",
                    source="panel_health",
                    session=session,
                )

    async def sync_services(self) -> int:
        async with session_scope() as session:
            return await provisioning.sync_services(session, limit=150, stale_minutes=15)

    async def retry_pending_provisioning(self) -> int:
        """Recover orders that were paid but never reached a node (crash/SIGKILL)."""
        if self._provision_lock.locked():
            return 0
        async with self._provision_lock:
            async with session_scope() as session:
                pending = await order_service.pending_provisioning(session, limit=10)
                ids = [order.id for order in pending]

            done = 0
            for order_id in ids:
                result = await provisioning.provision_order(order_id)
                if result.ok:
                    done += 1
                    await self._deliver(result.service_id)
                else:
                    await self._alert_failure(order_id, result.error or "خطای نامشخص")
            return done

    async def _deliver(self, service_id: int | None) -> None:
        if service_id is None:
            return
        from app.services.delivery import delivery

        async with session_scope() as session:
            service = await session.get(Service, service_id)
            if service is None:
                return
            order = (
                (await session.execute(select(Order).where(Order.service_id == service_id).order_by(Order.id.desc())))
                .scalars()
                .first()
            )
            intro = None
            if order is not None:
                intro = await delivery.purchase_success_text(session, service, order.order_code)
            await delivery.deliver_service(session, service, intro=intro)

    async def _alert_failure(self, order_id: int, reason: str) -> None:
        async with session_scope() as session:
            order = await session.get(Order, order_id)
            if order is None:
                return
            await notifier.to_admins(
                session,
                "⚠️ <b>خطا در ساخت سرویس</b>\n\n"
                f"سفارش: <code>{order.order_code}</code>\n"
                f"دلیل: {reason}\n\n"
                "از پنل مدیریت می‌توانید دوباره تلاش کنید.",
            )
            user = order.user
            if user is not None:
                body = await texts.get("buy.failed", session, order=order.order_code, reason=reason)
                await notifier.to_user(user, body)

    # -- customer notifications -------------------------------------------
    async def send_reminders(self) -> int:
        """Expiry reminders (N days before, 1 day before, and on expiry)."""
        if not app_settings.get_bool("notify.user_expiry", True):
            return 0
        days_before = app_settings.get_int("notify.expiry_days", 3)
        horizon = now_utc() + timedelta(days=days_before)
        sent = 0

        async with session_scope() as session:
            candidates = list(
                (
                    await session.execute(
                        select(Service).where(
                            Service.status == ServiceStatus.ACTIVE,
                            Service.is_test.is_(False),
                            Service.expires_at.is_not(None),
                            Service.expires_at <= horizon,
                        )
                    )
                ).scalars()
            )

            for service in candidates:
                user = service.user
                if user is None or user.is_blocked or user.is_bot_blocked:
                    continue
                remaining = service.expires_at - now_utc()  # type: ignore[operator]
                hours = remaining.total_seconds() / 3600

                if hours <= 0 and not service.notified_expired:
                    body = await texts.get("service.expired_notice", session, name=service.wg_username)
                    service.notified_expired = True
                    service.status = ServiceStatus.EXPIRED
                elif 0 < hours <= 24 and not service.notified_1d:
                    body = await texts.get(
                        "service.reminder_1d",
                        session,
                        name=service.wg_username,
                        expires=jalali_date(service.expires_at),
                    )
                    service.notified_1d = True
                    service.notified_3d = True
                elif hours > 24 and not service.notified_3d:
                    body = await texts.get(
                        "service.reminder_3d",
                        session,
                        name=service.wg_username,
                        days=str(max(int(hours // 24), 1)),
                        expires=jalali_date(service.expires_at),
                    )
                    service.notified_3d = True
                else:
                    continue

                if await notifier.to_user(user, body) is not None:
                    sent += 1
                await asyncio.sleep(0.05)

        if sent:
            log.info("Sent %d expiry reminders", sent)
        return sent

    async def send_traffic_alerts(self) -> int:
        """Warn at 80% usage and when the quota is exhausted."""
        if not app_settings.get_bool("notify.user_traffic", True):
            return 0
        sent = 0

        async with session_scope() as session:
            services = list(
                (
                    await session.execute(
                        select(Service).where(
                            Service.traffic_limit_bytes.is_not(None),
                            Service.status.in_((ServiceStatus.ACTIVE, ServiceStatus.TRAFFIC_EXCEEDED)),
                        )
                    )
                ).scalars()
            )

            for service in services:
                user = service.user
                if user is None or user.is_blocked or user.is_bot_blocked:
                    continue
                percent = service.usage_percent
                body: str | None = None

                if percent >= 100 and not service.notified_100pct:
                    body = await texts.get(
                        "service.traffic_100",
                        session,
                        name=service.wg_username,
                        used=format_bytes(service.traffic_used_bytes),
                        total=format_bytes(service.traffic_limit_bytes),
                    )
                    service.notified_100pct = True
                    service.notified_80pct = True
                elif 80 <= percent < 100 and not service.notified_80pct:
                    body = await texts.get(
                        "service.traffic_80",
                        session,
                        name=service.wg_username,
                        used=format_bytes(service.traffic_used_bytes),
                        total=format_bytes(service.traffic_limit_bytes),
                    )
                    service.notified_80pct = True
                else:
                    continue

                if await notifier.to_user(user, body) is not None:
                    sent += 1
                await asyncio.sleep(0.05)

        if sent:
            log.info("Sent %d traffic alerts", sent)
        return sent

    # -- backups & maintenance --------------------------------------------
    async def backup_database(self) -> str | None:
        """Run ``pg_dump`` into ``backups/`` and prune old files."""
        if not settings.backup_enabled:
            return None
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        stamp = now_utc().strftime("%Y%m%d-%H%M%S")
        target = BACKUP_DIR / f"wgguard-{stamp}.sql"

        env = {
            "PGPASSWORD": settings.postgres_password,
            "PGHOST": settings.postgres_host,
            "PGPORT": str(settings.postgres_port),
            "PGUSER": settings.postgres_user,
            "PGDATABASE": settings.postgres_db,
        }
        try:
            process = await asyncio.create_subprocess_exec(
                "pg_dump",
                "--no-owner",
                "--clean",
                "--if-exists",
                "-f",
                str(target),
                env=env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except FileNotFoundError:
            log.warning("pg_dump not found — database backups are disabled")
            return None

        if process.returncode != 0:
            log.error("pg_dump failed: %s", (stderr or b"").decode(errors="replace")[:400])
            return None

        self._prune_backups()
        log.info("Database backup written to %s", target.name)
        return str(target)

    @staticmethod
    def _prune_backups() -> None:
        files = sorted(BACKUP_DIR.glob("wgguard-*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in files[max(settings.backup_keep, 1) :]:
            with suppress(OSError):  # pragma: no cover
                stale.unlink()

    async def send_scheduled_broadcasts(self) -> int:
        """Fire campaigns whose scheduled time has arrived."""
        from app.db.models import Broadcast, BroadcastStatus
        from app.services.broadcast import broadcasts

        started = 0
        async with session_scope() as session:
            rows = list(
                (
                    await session.execute(
                        select(Broadcast).where(
                            Broadcast.status == BroadcastStatus.DRAFT,
                            Broadcast.scheduled_at.is_not(None),
                            Broadcast.scheduled_at <= now_utc(),
                        )
                    )
                ).scalars()
            )
            ids = [row.id for row in rows]

        for broadcast_id in ids:
            if await broadcasts.start(broadcast_id):
                started += 1
        if started:
            log.info("Started %d scheduled broadcasts", started)
        return started

    async def cleanup(self) -> None:
        """Trim verbose tables so the database does not grow forever."""
        cutoff = now_utc() - timedelta(days=30)
        async with session_scope() as session:
            await session.execute(delete(SystemEvent).where(SystemEvent.created_at < cutoff))
            await session.execute(
                delete(SystemEvent).where(
                    SystemEvent.level == EventLevel.INFO, SystemEvent.created_at < cutoff - timedelta(days=30)
                )
            )
        cache.invalidate_all()

    async def warm_caches(self) -> None:
        """Reload settings/texts/appearance after a restart or a manual reset."""
        from app.services.appearance import appearance
        from app.services.settings_store import app_settings
        from app.services.texts import texts

        async with session_scope() as session:
            await app_settings.load(session, force=True)
            await texts.load(session, force=True)
            await appearance.load(session, force=True)


jobs = Jobs()


def humanize_expiry(service: Service) -> str:
    return humanize_delta(service.expires_at)


def backup_dir() -> Path:
    return BACKUP_DIR


__all__ = ["BACKUP_DIR", "Jobs", "backup_dir", "humanize_expiry", "jobs"]
