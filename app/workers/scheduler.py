"""APScheduler wiring.

One :class:`AsyncIOScheduler` owns every periodic task.  Intervals come from the
environment so an operator can slow things down on a small VPS without touching
code.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger
from app.workers.jobs import jobs

log = get_logger(__name__)


class Scheduler:
    """Thin wrapper so the app does not depend on APScheduler's API directly."""

    def __init__(self) -> None:
        self._scheduler: Any = None

    def start(self) -> None:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        if self._scheduler is not None:
            return
        scheduler = AsyncIOScheduler(timezone="UTC")
        health_interval = max(settings.panel_health_interval, 30)

        scheduler.add_job(
            self._wrap("expire_payments", jobs.expire_payments),
            "interval",
            minutes=5,
            id="expire_payments",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self._wrap("panel_health", jobs.panel_health),
            "interval",
            seconds=health_interval,
            id="panel_health",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self._wrap("sync_services", jobs.sync_services),
            "interval",
            minutes=10,
            id="sync_services",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self._wrap("retry_provisioning", jobs.retry_pending_provisioning),
            "interval",
            minutes=3,
            id="retry_provisioning",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self._wrap("reminders", jobs.send_reminders),
            "interval",
            minutes=30,
            id="reminders",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self._wrap("traffic_alerts", jobs.send_traffic_alerts),
            "interval",
            minutes=20,
            id="traffic_alerts",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self._wrap("scheduled_broadcasts", jobs.send_scheduled_broadcasts),
            "interval",
            minutes=1,
            id="scheduled_broadcasts",
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            self._wrap("cleanup", jobs.cleanup),
            "interval",
            hours=12,
            id="cleanup",
            max_instances=1,
            coalesce=True,
        )
        if settings.backup_enabled:
            scheduler.add_job(
                self._wrap("backup", jobs.backup_database),
                "interval",
                hours=settings.backup_interval_hours,
                id="backup",
                max_instances=1,
                coalesce=True,
            )

        scheduler.start()
        self._scheduler = scheduler
        log.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))

    @staticmethod
    def _wrap(name: str, coro_func):
        """Run a job, swallowing exceptions so one failure never kills the loop."""

        async def runner() -> None:
            try:
                result = coro_func()
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:  # pragma: no cover - shutdown
                raise
            except Exception as exc:
                log.exception("Scheduled job %s failed: %s", name, exc)
                from app.services.notifications import notifier

                await notifier.report_error(exc, source=f"job:{name}", notify=False)

        return runner

    def shutdown(self) -> None:
        if self._scheduler is not None:
            with suppress(Exception):  # pragma: no cover
                self._scheduler.shutdown(wait=False)
            self._scheduler = None
            log.info("Scheduler stopped")

    @property
    def running(self) -> bool:
        return self._scheduler is not None and getattr(self._scheduler, "running", False)


scheduler = Scheduler()


__all__ = ["Scheduler", "scheduler"]
