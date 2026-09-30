"""Provisioning: turning a paid order into a working VPN service.

Paid operations and service maintenance share this orchestration layer. Its
provisioning rule is: **a network failure must never create a second service.**

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    AppError,
    NotFoundError,
    PanelConflict,
    PanelError,
    PanelUnavailable,
    ProvisioningFailed,
)
from app.core.jalali import now_utc
from app.core.locales import default_text
from app.core.logging import get_logger
from app.core.money import days_to_seconds
from app.core.security import encrypt_secret
from app.db.locks import locked_session
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
from app.panels.base import PanelProvider
from app.panels.manager import panel_manager
from app.panels.models import PlanSpec, PurchaseResult, RemoteUser
from app.services.catalog import catalog
from app.services.notifications import notifier
from app.services.orders import order_service, order_snapshot_terms
from app.services.rewards import apply_order_rewards

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
        async with locked_session(0x57474F, order_id) as (session, acquire):
            order = await session.get(Order, order_id)
            if order is None:
                return ProvisionResult(order_id, ok=False, error="سفارش پیدا نشد.")
            if order.status == OrderStatus.COMPLETED:
                await apply_order_rewards(session, order)
                return ProvisionResult(order_id, ok=True, service_id=order.service_id)
            if order.status not in (OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.FAILED):
                return ProvisionResult(order_id, ok=False, error=f"سفارش در وضعیت {order.status.value} قابل ساخت نیست.")
            if order.attempts and now_utc() - (order.paid_at or order.created_at) >= timedelta(days=90):
                return ProvisionResult(order_id, ok=False, error=default_text("error.node_response"))
            if order.service_id:
                await acquire(0x574753, order.service_id)

            await order_service.mark_provisioning(session, order)
            await session.commit()
            try:
                service = await self._dispatch(session, order, acquire)
            except AppError as exc:
                await session.rollback()
                order = await session.get(Order, order_id)
                await order_service.mark_failed(session, order, exc.message)
                return ProvisionResult(order_id, ok=False, error=exc.message)
            except Exception as exc:  # pragma: no cover - defensive
                log.error("Unexpected provisioning failure for order %s (%s)", order_id, type(exc).__name__)
                await session.rollback()
                order = await session.get(Order, order_id)
                await notifier.record_event("error", type(exc).__name__, source="provisioning", session=session)
                message = default_text("error.node_response")
                await order_service.mark_failed(session, order, message)
                return ProvisionResult(order_id, ok=False, error=message)

            await order_service.mark_completed(session, order, service)
            await session.commit()
            await apply_order_rewards(session, order)
            service_id = service.id
            order_code = order.order_code

        log.info("Order %s provisioned (service #%s)", order_code, service_id)
        return ProvisionResult(order_id, ok=True, service_id=service_id)

    async def _dispatch(
        self, session: AsyncSession, order: Order, acquire: Callable[[int, int], Awaitable[None]]
    ) -> Service:
        if order.kind == OrderKind.RENEW:
            return await self._provision_renew(session, order)
        if order.kind == OrderKind.EXTRA_TRAFFIC:
            return await self._provision_extra_traffic(session, order)
        if order.kind == OrderKind.EXTRA_DEVICE:
            raise ProvisioningFailed(default_text("service.extra_device_unavailable"))
        return await self._provision_new(session, order, acquire)

    # ------------------------------------------------------------------
    # New service
    # ------------------------------------------------------------------
    async def _provision_new(
        self, session: AsyncSession, order: Order, acquire: Callable[[int, int], Awaitable[None]]
    ) -> Service:
        plan = await self._plan_for(session, order)
        saved = (order.meta or {}).get("purchase")
        panel_id = saved["panel_id"] if saved else order.panel_id if order.attempts > 1 else None
        panel = await session.get(Panel, panel_id) if panel_id else await panel_manager.pick_panel(session, plan)
        if panel is None:
            raise ProvisioningFailed(default_text("error.node_not_found"))
        provider = await panel_manager.provider_for(panel)

        recovered = await self._recover(provider, order.idempotency_key) if saved or order.attempts > 1 else None
        if recovered is not None and not recovered.username:
            username = saved["username"] if saved else await self._make_username(session, order, plan)
            recovered = replace(recovered, username=username)
        if recovered is None:
            await acquire(0x574750, panel.id)
            await session.refresh(panel)
            if not panel.is_active or (panel.max_services is not None and panel.service_count >= panel.max_services):
                raise PanelUnavailable(default_text("error.node_capacity"))
        plan_ref = (
            saved["plan_ref"]
            if saved
            else recovered.plan_ref
            if recovered
            else await self._ensure_order_plan(session, order, plan, panel, provider)
        )
        order.panel_id = panel.id
        await session.flush()

        result = recovered or await self._purchase_with_retry(session, provider, order, plan, panel, plan_ref)
        remote = await self._safe_get_user(provider, result.user_id)

        configs = [(device_id, await provider.device_config(device_id)) for device_id in result.all_device_ids]
        try:
            devices = {device.id: device for device in await provider.list_devices(result.user_id)}
        except Exception as exc:
            log.warning("Could not read device metadata for user %s (%s)", result.user_id, type(exc).__name__)
            devices = {}
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

        for index, (device_id, config) in enumerate(configs, start=1):
            device = devices.get(device_id)
            session.add(
                ServiceDevice(
                    service_id=service.id,
                    wg_device_id=device_id,
                    name=device.name if device and device.name else f"device-{index}",
                    ipv4_address=device.ipv4_address if device else None,
                    config_encrypted=encrypt_secret(config, purpose="config"),
                    is_active=True,
                )
            )

        await self._after_success(session, order, plan, panel)
        return service

    async def _ensure_order_plan(
        self,
        session: AsyncSession,
        order: Order,
        plan: Plan,
        panel: Panel,
        provider: PanelProvider,
    ) -> str:
        """An order owns its technical snapshot; catalog edits cannot change it."""
        meta = dict(order.meta or {})
        spec = PlanSpec(
            name=f"{order.plan_name} {order.order_code}"[:120],
            **order_snapshot_terms(order),
            start_policy=meta.get("start_policy", "immediate"),
            interface_ref=meta.get("interface_id") or panel.default_interface_id,
        )
        reference = await provider.ensure_plan(spec, existing_ref=meta.get("node_plan_ref"))
        order.meta = {**meta, "node_plan_ref": reference}
        # Keep the legacy catalog reference useful to operators. Purchases never
        # trust it: it may point to another node or have been edited since payment.
        if not plan.wg_plan_id:
            plan.wg_plan_id = reference
        await session.flush()
        return reference

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
        saved = (order.meta or {}).get("purchase")
        username = saved["username"] if saved else await self._make_username(session, order, plan)
        # The key follows the *payload*: it stays ``base`` for every retry of the
        # same request and only rotates once the node has definitively refused
        # it (a taken username), because a new payload must not be answered from
        # the idempotency cache of the old one.
        key = saved["key"] if saved else base
        variant = saved.get("variant", 0) if saved else 0
        if saved:
            recovered = await self._recover(provider, key)
            if recovered is not None:
                return replace(recovered, username=recovered.username or username)

        for attempt in range(MAX_USERNAME_ATTEMPTS):
            order.idempotency_key = key
            order.meta = {
                **(order.meta or {}),
                "purchase": {
                    "panel_id": panel.id,
                    "plan_ref": plan_ref,
                    "username": username,
                    "key": key,
                    "variant": variant,
                },
            }
            # Persist the exact request before an external mutation. The session
            # advisory lock remains held across this transaction boundary.
            await session.commit()

            try:
                result = await provider.purchase(
                    plan_ref=plan_ref, username=username, device_name="device-1", idempotency_key=key
                )
            except PanelConflict as exc:
                if self._is_username_conflict(exc):
                    log.info("Username %s taken on %s — retrying with a new username", username, panel.name)
                    username = self._variant_username(username, variant)
                    variant += 1
                    key = f"{base}-r{variant}"
                    continue
                raise ProvisioningFailed(self._explain(exc)) from exc
            except PanelUnavailable as exc:
                # Ambiguous: the node may have committed before the connection died.
                recovered = await self._recover(provider, key)
                if recovered is not None:
                    log.info("Recovered committed purchase for order %s", order.order_code)
                    return replace(recovered, username=recovered.username or username)
                if attempt >= MAX_USERNAME_ATTEMPTS - 1:
                    raise ProvisioningFailed(self._explain(exc)) from exc
                log.info("Retrying purchase for order %s with the same idempotency key", order.order_code)
                continue
            except PanelError as exc:
                raise ProvisioningFailed(self._explain(exc)) from exc
            except Exception as exc:  # network layer surprises
                recovered = await self._recover(provider, key)
                if recovered is not None:
                    log.info("Recovered committed purchase for order %s after an unexpected error", order.order_code)
                    return replace(recovered, username=recovered.username or username)
                log.error("Unexpected purchase error for order %s (%s)", order.order_code, type(exc).__name__)
                raise ProvisioningFailed(
                    "ارتباط با پنل ناگهان قطع شد و سفارش نیمه‌کاره ماند. چند دقیقه بعد دوباره تلاش کنید."
                ) from exc

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
                    device_ids=result.device_ids,
                )
            return result

        raise ProvisioningFailed("نام کاربری یکتا برای این سرویس پیدا نشد. لطفاً دوباره تلاش کنید.")

    async def _recover(self, provider: PanelProvider, key: str) -> PurchaseResult | None:
        """Unknown recovery state must propagate, never become 'not committed'."""
        return await provider.recover_purchase(key)

    # ------------------------------------------------------------------
    # Renewals / add-ons
    # ------------------------------------------------------------------
    async def _provision_renew(self, session: AsyncSession, order: Order) -> Service:
        service = await self._service_for(session, order)
        _, _panel, provider = await panel_manager.find_service(session, service.id)

        provider.require("next_plan")
        remote = await provider.get_user(service.wg_user_id)
        if remote.traffic_limit_bytes is None and remote.duration_seconds is None and remote.expires_at is None:
            raise ProvisioningFailed(default_text("service.renew_unavailable"))
        meta = dict(order.meta or {})
        reference = meta.get("node_plan_ref")
        await self.reconcile_next_plan(service, provider)
        pending = (service.meta or {}).get("paid_next_plan")
        if pending and pending["order_id"] != order.id:
            raise ProvisioningFailed(default_text("service.renew_pending"))
        queued = await provider.next_plan(service.wg_user_id)
        activations = await provider.plan_activations(service.wg_user_id) if reference else []
        activated = any(item.plan_ref == reference for item in activations)
        if not activated and queued and queued.plan_ref != reference:
            raise ProvisioningFailed(default_text("service.renew_pending"))
        if not reference:
            plan = await self._plan_for(session, order)
            panel = await session.get(Panel, service.panel_id)
            reference = await self._ensure_order_plan(session, order, plan, panel, provider)
        if not activated and not queued:
            # The ordinary replay cache lasts only 24h. Persist intent and reconcile
            # queue + activation history before ever resending a paid successor.
            service.meta = {**(service.meta or {}), "paid_next_plan": {"order_id": order.id, "plan_ref": reference}}
            await session.commit()
            await provider.queue_next_plan(
                service.wg_user_id,
                reference,
                idempotency_key=self._idem_base(order),
            )
        if not activated:
            service.meta = {**(service.meta or {}), "paid_next_plan": {"order_id": order.id, "plan_ref": reference}}
            service.auto_renew = True
        else:
            order.meta = {**(order.meta or {}), "renewal_activated": True}
        plan = await self._plan_for(session, order, required=False)
        if plan is not None:
            await self._after_success(session, order, plan, None)
        await session.flush()
        return service

    async def _provision_extra_traffic(self, session: AsyncSession, order: Order) -> Service:
        service = await self._service_for(session, order)
        _, _panel, provider = await panel_manager.find_service(session, service.id)

        extra_bytes = self._limit_bytes(order) or 0
        if extra_bytes <= 0:
            raise ProvisioningFailed("حجم اضافه برای این سفارش تعیین نشده است.")
        remote = await provider.top_up_quota(
            service.wg_user_id,
            extra_bytes,
            idempotency_key=self._idem_base(order),
        )
        self.apply_remote_state(service, remote)
        # Clear the traffic alerts so the customer gets warned again next time.
        service.notified_80pct = False
        service.notified_100pct = False
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
            # Random URL-safe keys may contain '-r'; it is not evidence of a
            # retry suffix. Legacy keys must retain their complete identity too.
            base = order.idempotency_key
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
        return code == "USERNAME_EXISTS"

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
        return order_snapshot_terms(order)["traffic_limit_bytes"]

    @staticmethod
    def _duration_seconds(order: Order) -> int | None:
        return days_to_seconds(order.duration_days)

    async def _safe_get_user(self, provider: PanelProvider, user_id: str) -> RemoteUser | None:
        """Read the user back, tolerating *any* failure.

        The account already exists on the node at this point; a read-back that
        fails (transport hiccup, a node answering a shape this build does not
        know yet) must not mark a committed purchase as failed — the caller
        falls back to the values it already has.
        """
        try:
            return await provider.get_user(user_id)
        except Exception as exc:
            log.warning("Could not read back user %s (continuing with local values): %s", user_id, type(exc).__name__)
            return None

    async def _safe_subscription(self, provider: PanelProvider, user_id: str) -> str | None:
        try:
            link = await provider.subscription_link(user_id)
        except Exception as exc:
            log.warning("Could not read subscription link for %s: %s", user_id, type(exc).__name__)
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
        await session.execute(update(Plan).where(Plan.id == plan.id).values(sales_count=Plan.sales_count + 1))
        if panel is not None:
            await session.execute(
                update(Panel).where(Panel.id == panel.id).values(service_count=Panel.service_count + 1)
            )
        if not (order.meta or {}).get("stock_reserved"):
            await catalog.decrement_stock(session, plan)
        await session.flush()

    def apply_remote_state(self, service: Service, remote: RemoteUser) -> None:
        """Copy authoritative node state onto a local service row."""
        service.remote_status = remote.status
        if service.status != ServiceStatus.DELETED:
            service.status = self._service_status(remote)
        service.traffic_limit_bytes = remote.traffic_limit_bytes
        service.traffic_used_bytes = remote.traffic_used_bytes
        service.speed_limit_down_kbps = remote.speed_limit_down_kbps
        service.speed_limit_up_kbps = remote.speed_limit_up_kbps
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
        await self.reconcile_next_plan(service, provider)
        await session.flush()
        return service

    async def reconcile_next_plan(self, service: Service, provider: PanelProvider) -> None:
        pending = (service.meta or {}).get("paid_next_plan")
        if not pending:
            return
        queued = await provider.next_plan(service.wg_user_id)
        if queued and queued.plan_ref == pending["plan_ref"]:
            service.auto_renew = True
            service.meta = {**service.meta, "paid_next_plan": {**pending, "state": queued.state}}
            return
        activations = await provider.plan_activations(service.wg_user_id)
        if any(item.plan_ref == pending["plan_ref"] for item in activations):
            meta = dict(service.meta)
            meta["last_paid_next_plan"] = meta.pop("paid_next_plan")
            service.meta = meta
            service.auto_renew = False
            service.is_test = False
            self.reset_notifications(service)
        else:
            service.meta = {**service.meta, "paid_next_plan": {**pending, "state": "needs_review"}}

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
