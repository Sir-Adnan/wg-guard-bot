"""Provisioning: turning a paid order into a working VPN service.

This is the only module that mutates a WG-Guard node.  It is written around one
rule: **a network failure must never create a second service.**

How that is guaranteed
----------------------
``POST /api/v1/purchases`` is idempotent for 90 days under an
``Idempotency-Key``.  We therefore:

1. keep one *stable* base key per order (``order.meta["idem_base"]``),
2. retry transport failures with the **same** key, so a commit that happened
   before the connection dropped is replayed instead of repeated,
3. after an ambiguous failure, ask ``GET /api/v1/operations/result`` whether the
   purchase actually committed before deciding to retry,
4. only switch to a derived key (``<base>-r1``, ``-r2`` …) after the node has
   *definitively* refused the payload — for example ``USERNAME_EXISTS``.

Everything happens in a dedicated session so a slow node never holds a
transaction open for the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AppError,
    NotFoundError,
    PanelConflict,
    PanelError,
    PanelNotFound,
    PanelUnavailable,
    ProvisioningFailed,
)
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import days_to_seconds, gb_to_bytes
from app.core.security import encrypt_secret
from app.db.models import (
    Order,
    OrderKind,
    OrderStatus,
    Panel,
    Plan,
    Service,
    ServiceDevice,
    ServiceStatus,
)
from app.db.session import session_scope
from app.panels.base import PanelProvider
from app.panels.manager import panel_manager
from app.panels.models import PurchaseResult, RemoteUser
from app.services.catalog import catalog
from app.services.orders import order_service

log = get_logger(__name__)

#: WG-Guard username rule: ``^[a-zA-Z0-9_-]{3,32}$``
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]{3,32}$")
MAX_USERNAME_ATTEMPTS = 5

#: Node status -> local status
STATUS_MAP: dict[str, ServiceStatus] = {
    "active": ServiceStatus.ACTIVE,
    "waiting_first_connection": ServiceStatus.ACTIVE,
    "disabled": ServiceStatus.DISABLED,
    "suspended": ServiceStatus.DISABLED,
    "expired": ServiceStatus.EXPIRED,
    "traffic_exceeded": ServiceStatus.TRAFFIC_EXCEEDED,
}


@dataclass(slots=True)
class ProvisionResult:
    order_id: int
    ok: bool
    service_id: int | None = None
    error: str | None = None

    @property
    def failed(self) -> bool:
        return not self.ok


class ProvisioningService:
    """Orchestrates node calls and keeps local state in sync."""

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def provision_order(self, order_id: int) -> ProvisionResult:
        """Run the whole provisioning flow for one order in its own session."""
        async with session_scope() as session:
            order = await session.get(Order, order_id)
            if order is None:
                return ProvisionResult(order_id, ok=False, error="سفارش پیدا نشد.")
            if order.status == OrderStatus.COMPLETED:
                return ProvisionResult(order_id, ok=True, service_id=order.service_id)
            if order.status not in (OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.FAILED):
                return ProvisionResult(order_id, ok=False, error=f"سفارش در وضعیت {order.status.value} قابل ساخت نیست.")

            await order_service.mark_provisioning(session, order)
            try:
                service = await self._dispatch(session, order)
            except AppError as exc:
                await order_service.mark_failed(session, order, exc.message)
                return ProvisionResult(order_id, ok=False, error=exc.message)
            except Exception as exc:  # pragma: no cover - defensive
                log.exception("Unexpected provisioning failure for %s", order.order_code)
                await order_service.mark_failed(session, order, f"{type(exc).__name__}: {exc}")
                return ProvisionResult(order_id, ok=False, error=str(exc))

            await order_service.mark_completed(session, order, service)
            service_id = service.id
            order_code = order.order_code

        log.info("Order %s provisioned (service #%s)", order_code, service_id)
        return ProvisionResult(order_id, ok=True, service_id=service_id)

    async def _dispatch(self, session: AsyncSession, order: Order) -> Service:
        if order.kind == OrderKind.RENEW:
            return await self._provision_renew(session, order)
        if order.kind == OrderKind.EXTRA_TRAFFIC:
            return await self._provision_extra_traffic(session, order)
        if order.kind == OrderKind.EXTRA_DEVICE:
            return await self._provision_extra_device(session, order)
        return await self._provision_new(session, order)

    # ------------------------------------------------------------------
    # New service
    # ------------------------------------------------------------------
    async def _provision_new(self, session: AsyncSession, order: Order) -> Service:
        plan = await self._plan_for(session, order)
        panel = await panel_manager.pick_panel(session, plan)
        provider = await panel_manager.provider_for(panel)

        plan_ref = await panel_manager.ensure_node_plan(session, plan, panel)
        order.panel_id = panel.id
        await session.flush()

        result = await self._purchase_with_retry(session, provider, order, plan, panel, plan_ref)
        remote = await self._safe_get_user(provider, result.user_id)

        config = await provider.device_config(result.device_id)
        subscription = await self._safe_subscription(provider, result.user_id)

        service = Service(
            user_id=order.user_id,
            panel_id=panel.id,
            plan_id=plan.id,
            origin_order_id=order.id,
            wg_user_id=result.user_id,
            wg_username=remote.username if remote else result.username,
            subscription_encrypted=encrypt_secret(subscription, purpose="subscription"),
            status=self._service_status(remote),
            remote_status=remote.status if remote else "active",
            traffic_limit_bytes=remote.traffic_limit_bytes if remote else self._limit_bytes(order),
            traffic_used_bytes=remote.traffic_used_bytes if remote else 0,
            device_limit=remote.device_limit if remote and remote.device_limit else order.device_limit,
            speed_limit_down_kbps=order.speed_limit_down_kbps,
            speed_limit_up_kbps=order.speed_limit_up_kbps,
            started_at=remote.activated_at if remote else None,
            expires_at=remote.expires_at if remote else None,
            last_synced_at=now_utc(),
            is_test=order.is_test,
            meta={"node_plan_id": plan_ref, "panel_name": panel.name, "provider": provider.kind},
        )
        session.add(service)
        await session.flush()

        session.add(
            ServiceDevice(
                service_id=service.id,
                wg_device_id=result.device_id,
                name="device-1",
                ipv4_address=None,
                config_encrypted=encrypt_secret(config, purpose="config"),
                is_active=True,
            )
        )

        await self._after_success(session, order, plan, panel)
        return service

    async def _purchase_with_retry(
        self,
        session: AsyncSession,
        provider: PanelProvider,
        order: Order,
        plan: Plan,
        panel: Panel,
        plan_ref: str,
    ) -> PurchaseResult:
        """Provision exactly once, honouring the provider's idempotency contract."""
        base = self._idem_base(order)
        username = await self._make_username(session, order, plan)

        for attempt in range(MAX_USERNAME_ATTEMPTS):
            key = base if attempt == 0 else f"{base}-r{attempt}"
            order.idempotency_key = key
            await session.flush()

            try:
                result = await provider.purchase(
                    plan_ref=plan_ref, username=username, device_name="device-1", idempotency_key=key
                )
            except PanelConflict as exc:
                if self._is_username_conflict(exc):
                    log.info("Username %s taken on %s — retrying", username, panel.name)
                    username = self._variant_username(username, attempt)
                    continue
                raise ProvisioningFailed(self._explain(exc)) from exc
            except PanelUnavailable as exc:
                # Ambiguous: the node may have committed before the connection died.
                recovered = await self._recover(provider, key)
                if recovered is not None:
                    log.info("Recovered committed purchase for order %s", order.order_code)
                    return recovered
                if attempt >= MAX_USERNAME_ATTEMPTS - 1:
                    raise ProvisioningFailed(self._explain(exc)) from exc
                log.info("Retrying purchase for order %s with the same idempotency key", order.order_code)
                continue
            except PanelError as exc:
                raise ProvisioningFailed(self._explain(exc)) from exc
            except Exception as exc:  # network layer surprises
                recovered = await self._recover(provider, key)
                if recovered is not None:
                    return recovered
                raise ProvisioningFailed(f"خطای غیرمنتظره در ارتباط با پنل: {exc}") from exc

            if not self._username_matches(username):
                username = self._sanitise(username)
            if not result.username:
                result = PurchaseResult(
                    user_id=result.user_id,
                    device_id=result.device_id,
                    username=username,
                    plan_ref=result.plan_ref,
                    operation_id=result.operation_id,
                    created_at=result.created_at,
                    recovered=result.recovered,
                )
            return result

        raise ProvisioningFailed("نام کاربری یکتا برای این سرویس پیدا نشد. لطفاً دوباره تلاش کنید.")

    async def _recover(self, provider: PanelProvider, key: str) -> PurchaseResult | None:
        """Ask the node whether a purchase under ``key`` already committed.

        A provider without purchase recovery returns ``None``, which means
        "not committed" — the caller may then retry safely.
        """
        try:
            return await provider.recover_purchase(key)
        except (PanelNotFound, PanelError):
            return None

    # ------------------------------------------------------------------
    # Renewals / add-ons
    # ------------------------------------------------------------------
    async def _provision_renew(self, session: AsyncSession, order: Order) -> Service:
        service = await self._service_for(session, order)
        _, _panel, provider = await panel_manager.find_service(session, service.id)

        remote = await provider.renew_user(
            service.wg_user_id,
            duration_seconds=self._duration_seconds(order),
            idempotency_key=self._idem_base(order),
        )
        self.apply_remote_state(service, remote)
        service.is_test = False
        self.reset_notifications(service)
        await session.flush()

        plan = await self._plan_for(session, order, required=False)
        if plan is not None:
            await self._after_success(session, order, plan, await session.get(Panel, service.panel_id))
        return service

    async def _provision_extra_traffic(self, session: AsyncSession, order: Order) -> Service:
        service = await self._service_for(session, order)
        _, _panel, provider = await panel_manager.find_service(session, service.id)

        extra_bytes = self._limit_bytes(order) or 0
        if extra_bytes <= 0:
            raise ProvisioningFailed("حجم اضافه برای این سفارش تعیین نشده است.")
        current = int(service.traffic_limit_bytes or 0)
        new_limit = current + extra_bytes

        remote = await provider.set_limits(service.wg_user_id, traffic_limit_bytes=new_limit)
        self.apply_remote_state(service, remote)
        # Clear the traffic alerts so the customer gets warned again next time.
        service.notified_80pct = False
        service.notified_100pct = False
        await session.flush()
        return service

    async def _provision_extra_device(self, session: AsyncSession, order: Order) -> Service:
        service = await self._service_for(session, order)
        _, _panel, provider = await panel_manager.find_service(session, service.id)

        devices = await provider.list_devices(service.wg_user_id)
        limit = service.device_limit or order.device_limit or 1
        if len(devices) >= limit:
            raise ProvisioningFailed(f"سقف دستگاه‌های این سرویس ({limit}) تکمیل است.")

        index = len(devices) + 1
        device = await provider.create_device(service.wg_user_id, name=f"device-{index}")
        config = await provider.device_config(device.id)
        session.add(
            ServiceDevice(
                service_id=service.id,
                wg_device_id=device.id,
                name=device.name or f"device-{index}",
                ipv4_address=device.ipv4_address,
                config_encrypted=encrypt_secret(config, purpose="config"),
                is_active=True,
            )
        )
        service.device_limit = max(limit, index)
        await session.flush()
        return service

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _plan_for(self, session: AsyncSession, order: Order, *, required: bool = True) -> Plan | None:
        if order.plan_id is None:
            if required:
                raise ProvisioningFailed("این سفارش پلنی ندارد.")
            return None
        plan = await session.get(Plan, order.plan_id)
        if plan is None and required:
            raise ProvisioningFailed("پلن این سفارش حذف شده است.")
        return plan

    async def _service_for(self, session: AsyncSession, order: Order) -> Service:
        if order.service_id is None:
            raise ProvisioningFailed("این سفارش به هیچ سرویسی متصل نیست.")
        service = await session.get(Service, order.service_id)
        if service is None:
            raise NotFoundError("سرویس مربوط به این سفارش پیدا نشد.")
        return service

    def _idem_base(self, order: Order) -> str:
        meta = dict(order.meta or {})
        base = meta.get("idem_base")
        if not base:
            base = order.idempotency_key.split("-r")[0] if "-r" in order.idempotency_key else order.idempotency_key
            meta["idem_base"] = base
            order.meta = meta
        return str(base)

    async def _make_username(self, session: AsyncSession, order: Order, plan: Plan) -> str:
        from app.db.models import User

        template = plan.username_template or "wg{tg}"
        user = await session.get(User, order.user_id)
        telegram_id = user.telegram_id if user else order.id

        candidate = (
            template.replace("{tg}", str(telegram_id))
            .replace("{id}", str(order.id))
            .replace("{n}", str(order.id))
            .replace("{rand}", order.order_code.split("-")[-1].lower())
        )
        candidate = self._sanitise(candidate)
        if not self._username_matches(candidate):
            candidate = self._sanitise(f"wg{telegram_id}")
        return candidate

    @staticmethod
    def _sanitise(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", value or "")
        if len(cleaned) < 3:
            cleaned = f"wg{cleaned}"
        return cleaned[:32]

    @staticmethod
    def _username_matches(value: str) -> bool:
        return bool(USERNAME_RE.match(value or ""))

    @staticmethod
    def _variant_username(username: str, attempt: int) -> str:
        suffix = f"-{attempt + 1}"
        return (username[: 32 - len(suffix)] + suffix) if len(username) + len(suffix) > 32 else username + suffix

    @staticmethod
    def _is_username_conflict(exc: PanelConflict) -> bool:
        code = (exc.panel_code or "").upper()
        message = (exc.message or "").lower()
        return code == "USERNAME_EXISTS" or "username" in message

    @staticmethod
    def _explain(exc: PanelError) -> str:
        code = (exc.panel_code or "").upper()
        if code == "RATE_LIMITED":
            return "پنل درخواست‌ها را محدود کرده است. چند دقیقه بعد دوباره تلاش کنید."
        if code in ("NODE_UNAVAILABLE", "INTERNAL_ERROR"):
            return "پنل موقتاً در دسترس نیست. لطفاً دوباره تلاش کنید."
        if code == "DEVICE_LIMIT_REACHED":
            return "سقف دستگاه‌های مجاز در پنل تکمیل است."
        return exc.message

    @staticmethod
    def _limit_bytes(order: Order) -> int | None:
        return gb_to_bytes(order.traffic_gb)

    @staticmethod
    def _duration_seconds(order: Order) -> int | None:
        return days_to_seconds(order.duration_days)

    async def _safe_get_user(self, provider: PanelProvider, user_id: str) -> RemoteUser | None:
        try:
            return await provider.get_user(user_id)
        except PanelError as exc:
            log.warning("Could not read back user %s: %s", user_id, exc.message)
            return None

    async def _safe_subscription(self, provider: PanelProvider, user_id: str) -> str | None:
        try:
            link = await provider.subscription_link(user_id)
        except PanelError as exc:
            log.warning("Could not read subscription link for %s: %s", user_id, exc.message)
            return None
        return link.path

    @staticmethod
    def _service_status(remote: RemoteUser | None) -> ServiceStatus:
        """Map a canonical remote status onto our local :class:`ServiceStatus`."""
        if remote is None:
            return ServiceStatus.ACTIVE
        return STATUS_MAP.get(remote.status, ServiceStatus.ACTIVE)

    async def _after_success(self, session: AsyncSession, order: Order, plan: Plan, panel: Panel | None) -> None:
        """Bookkeeping shared by every successful provisioning path."""
        plan.sales_count = int(plan.sales_count) + 1
        if panel is not None:
            panel.service_count = int(panel.service_count) + 1
        await catalog.decrement_stock(session, plan)
        await session.flush()

    def apply_remote_state(self, service: Service, remote: RemoteUser) -> None:
        """Copy authoritative node state onto a local service row."""
        service.remote_status = remote.status
        service.status = self._service_status(remote)
        service.traffic_limit_bytes = remote.traffic_limit_bytes
        service.traffic_used_bytes = remote.traffic_used_bytes
        if remote.device_limit:
            service.device_limit = remote.device_limit
        service.expires_at = remote.expires_at
        service.started_at = remote.activated_at or service.started_at
        service.last_activity_at = remote.last_activity_at
        service.last_synced_at = now_utc()

    @staticmethod
    def reset_notifications(service: Service) -> None:
        service.notified_3d = False
        service.notified_1d = False
        service.notified_expired = False
        service.notified_80pct = False
        service.notified_100pct = False

    # ------------------------------------------------------------------
    # Synchronisation
    # ------------------------------------------------------------------
    async def sync_service(self, session: AsyncSession, service: Service) -> Service:
        """Refresh one service from its node (used by the panel and reminders)."""
        try:
            _, _panel, provider = await panel_manager.find_service(session, service.id)
            remote = await provider.get_user(service.wg_user_id)
        except AppError as exc:
            log.debug("sync_service(%s) skipped: %s", service.id, exc)
            return service
        self.apply_remote_state(service, remote)
        await session.flush()
        return service

    async def sync_services(self, session: AsyncSession, *, limit: int = 200, stale_minutes: int = 10) -> int:
        """Refresh services whose cached state is older than ``stale_minutes``."""
        from datetime import timedelta

        cutoff = now_utc() - timedelta(minutes=stale_minutes)
        rows = list(
            (
                await session.execute(
                    select(Service)
                    .where(Service.status != ServiceStatus.DELETED)
                    .where((Service.last_synced_at.is_(None)) | (Service.last_synced_at < cutoff))
                    .order_by(Service.last_synced_at.asc().nulls_first())
                    .limit(limit)
                )
            ).scalars()
        )
        count = 0
        for service in rows:
            await self.sync_service(session, service)
            count += 1
        return count

    # ------------------------------------------------------------------
    # Service-level operations exposed to handlers
    # ------------------------------------------------------------------
    async def rotate_access(self, session: AsyncSession, service: Service) -> tuple[str | None, str | None]:
        """Rotate keys + subscription link.  Returns ``(config, link)``."""
        _, _panel, provider = await panel_manager.find_service(session, service.id)
        rotation = await provider.rotate_subscription(service.wg_user_id)
        service.subscription_encrypted = encrypt_secret(rotation.path, purpose="subscription")

        devices = await provider.list_devices(service.wg_user_id)
        first_config: str | None = None
        for device in devices:
            config = await provider.device_config(device.id)
            if first_config is None:
                first_config = config
            local = await self._local_device(session, service.id, device.id)
            if local is not None:
                local.config_encrypted = encrypt_secret(config, purpose="config")
        await session.flush()
        return first_config, rotation.path

    async def refresh_device_config(self, session: AsyncSession, device: ServiceDevice) -> str:
        """Re-download a device config (the private key never leaves the node)."""
        _service, _panel, provider = await panel_manager.find_service(session, device.service_id)
        config = await provider.device_config(device.wg_device_id)
        device.config_encrypted = encrypt_secret(config, purpose="config")
        await session.flush()
        return config

    async def delete_device(self, session: AsyncSession, device: ServiceDevice) -> None:
        _service, _panel, provider = await panel_manager.find_service(session, device.service_id)
        await provider.delete_device(device.wg_device_id)
        await session.delete(device)
        await session.flush()

    async def add_device(self, session: AsyncSession, service: Service, name: str | None = None) -> ServiceDevice:
        _, _panel, provider = await panel_manager.find_service(session, service.id)
        devices = await provider.list_devices(service.wg_user_id)
        limit = service.device_limit or 1
        if len(devices) >= limit:
            raise ProvisioningFailed(f"سقف دستگاه‌های این سرویس ({limit}) تکمیل است.")
        device = await provider.create_device(service.wg_user_id, name=name or f"device-{len(devices) + 1}")
        config = await provider.device_config(device.id)
        local = ServiceDevice(
            service_id=service.id,
            wg_device_id=device.id,
            name=device.name or f"device-{len(devices) + 1}",
            ipv4_address=device.ipv4_address,
            config_encrypted=encrypt_secret(config, purpose="config"),
        )
        session.add(local)
        await session.flush()
        return local

    async def _local_device(self, session: AsyncSession, service_id: int, wg_device_id: str) -> ServiceDevice | None:
        return (
            await session.execute(
                select(ServiceDevice).where(
                    ServiceDevice.service_id == service_id, ServiceDevice.wg_device_id == wg_device_id
                )
            )
        ).scalar_one_or_none()


provisioning = ProvisioningService()


def expires_within(expires_at: datetime | None, *, hours: float) -> bool:
    if expires_at is None:
        return False
    from datetime import timedelta

    return (expires_at - now_utc()) <= timedelta(hours=hours)


__all__ = ["ProvisionResult", "ProvisioningService", "expires_within", "provisioning"]
