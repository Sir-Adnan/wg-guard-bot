"""WG-Guard adapter.

Thin translation layer between the canonical port
(:class:`app.panels.base.PanelProvider`) and the vendor HTTP client
(:class:`app.panels.client.WGGuardClient`).  It contains **no business rules** —
only field mapping, status normalisation and capability declarations.

If you are adding a different backend, read this file together with
``app/panels/providers/example.py``: the example is a documented skeleton of the
same shape.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import PanelError, PanelNotFound, PanelUnavailable, PanelValidation
from app.core.locales import default_text
from app.core.logging import get_logger
from app.panels.base import (
    CAP_ATOMIC_PURCHASE,
    CAP_INTERFACES,
    CAP_MULTI_DEVICE_PURCHASE,
    CAP_NEXT_PLAN,
    CAP_NODE_QR,
    CAP_PLAN_SYNC,
    CAP_PURCHASE_RECOVERY,
    CAP_QUOTA_TOP_UP,
    CAP_SUBSCRIPTION_ROTATE,
    CAP_TRAFFIC_SERIES,
    CAP_TRAFFIC_WRITE,
    CAP_WEBHOOKS,
    PanelProvider,
)
from app.panels.client import WGGuardClient
from app.panels.models import (
    NodeInfo,
    PlanActivation,
    PlanSpec,
    ProviderHealth,
    PurchaseResult,
    QueuedPlan,
    RemoteDevice,
    RemoteInterface,
    RemoteStatus,
    RemoteUser,
    SubscriptionLink,
)
from app.panels.registry import register
from app.panels.schemas import PlanPatch
from app.panels.schemas import User as WgUser

log = get_logger(__name__)

#: Vendor status -> canonical status.  ``suspended`` is a vendor-only state that
#: behaves like a manual disable.
STATUS_MAP: dict[str, RemoteStatus] = {
    "active": "active",
    "waiting_first_connection": "waiting_first_connection",
    "disabled": "disabled",
    "suspended": "disabled",
    "expired": "expired",
    "traffic_exceeded": "traffic_exceeded",
}


def normalise_status(value: str | None) -> RemoteStatus:
    return STATUS_MAP.get((value or "").strip(), "disabled")


def to_remote_user(user: WgUser) -> RemoteUser:
    status = normalise_status(user.status)
    if not user.enabled and status in ("active", "waiting_first_connection"):
        status = "disabled"
    return RemoteUser(
        id=user.id,
        username=user.username,
        status=status,
        enabled=user.enabled,
        traffic_limit_bytes=user.traffic_limit_bytes,
        traffic_used_bytes=user.traffic_used_total,
        duration_seconds=user.duration_seconds,
        device_limit=user.device_limit,
        speed_limit_down_kbps=user.speed_limit_down_kbps,
        speed_limit_up_kbps=user.speed_limit_up_kbps,
        expires_at=user.expires_at,
        activated_at=user.activated_at,
        last_activity_at=user.last_activity_at,
        plan_ref=user.template_id,
        raw=user.model_dump(),
    )


def to_remote_device(device: Any) -> RemoteDevice:
    return RemoteDevice(
        id=device.id,
        user_id=device.user_id,
        name=device.name or "",
        ipv4_address=device.ipv4_address,
        enabled=device.enabled,
        public_key=device.public_key,
        last_handshake_at=device.last_handshake_at,
        rx_bytes=device.rx_bytes,
        tx_bytes=device.tx_bytes,
        raw=device.model_dump(),
    )


@register
class WGGuardProvider(PanelProvider):
    """Adapter for a WG-Guard (AmneziaWG) node."""

    kind = "wg_guard"
    label = "WG-Guard"
    description = "پنل WG-Guard (AmneziaWG) — پشتیبانی کامل شامل ساخت اتمی سرویس و لینک اشتراک"
    capabilities = frozenset(
        {
            CAP_PLAN_SYNC,
            CAP_ATOMIC_PURCHASE,
            CAP_MULTI_DEVICE_PURCHASE,
            CAP_PURCHASE_RECOVERY,
            CAP_NODE_QR,
            CAP_NEXT_PLAN,
            CAP_SUBSCRIPTION_ROTATE,
            CAP_TRAFFIC_WRITE,
            CAP_QUOTA_TOP_UP,
            CAP_TRAFFIC_SERIES,
            CAP_WEBHOOKS,
            CAP_INTERFACES,
        }
    )

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._client = WGGuardClient(
            base_url=self.base_url,
            token=self.token,
            timeout=self.timeout,
            connect_timeout=self.connect_timeout,
            max_retries=self.max_retries,
            transport=self.transport,  # type: ignore[arg-type]
        )

    @property
    def client(self) -> WGGuardClient:
        """Escape hatch for node-only features (settings, webhooks, telemetry)."""
        return self._client

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- discovery ---------------------------------------------------------
    async def health(self) -> ProviderHealth:
        try:
            node = await self._client.node()
        except PanelError as exc:
            return ProviderHealth(online=False, message=exc.message)

        enabled = [item for item in node.interfaces if item.enabled]
        degraded = not enabled
        return ProviderHealth(
            online=True,
            degraded=degraded,
            version=node.version,
            node_id=node.node_id,
            uptime_seconds=node.uptime_seconds,
            interface_count=len(enabled),
            message="هیچ اینترفیس فعالی روی نود وجود ندارد." if degraded else None,
        )

    async def node_info(self) -> NodeInfo:
        node = await self._client.node()
        return NodeInfo(
            provider_kind=self.kind,
            name=self.name,
            version=node.version,
            node_id=node.node_id,
            uptime_seconds=node.uptime_seconds,
            interfaces=tuple(
                RemoteInterface(
                    id=item.id,
                    name=item.name,
                    enabled=item.enabled,
                    listen_port=item.listen_port,
                    ipv4_subnet=item.ipv4_subnet,
                    devices=item.devices,
                )
                for item in node.interfaces
            ),
            raw=node.model_dump(),
        )

    # -- catalogue ---------------------------------------------------------
    def _plan_payload(self, spec: PlanSpec) -> PlanPatch:
        return PlanPatch(
            name=self._template_name(spec.name),
            traffic_limit_bytes=spec.traffic_limit_bytes,
            duration_seconds=spec.duration_seconds,
            start_policy="immediate" if spec.start_policy == "immediate" else "first_connection",
            device_limit=spec.device_limit,
            speed_limit_down_kbps=spec.speed_limit_down_kbps,
            speed_limit_up_kbps=spec.speed_limit_up_kbps,
            interface_id=spec.interface_ref,
            enabled=True,
        )

    @staticmethod
    def _template_name(name: str) -> str:
        # The contract counts UTF-8 bytes, not Python characters or codepoints.
        name = name.strip().encode("utf-8")[:64].decode("utf-8", errors="ignore").strip()
        if not name:
            raise PanelValidation(default_text("error.node_validation"))
        return name

    async def ensure_plan(self, spec: PlanSpec, *, existing_ref: str | None = None) -> str:
        payload = self._plan_payload(spec)

        if existing_ref:
            try:
                remote = await self._client.get_plan(existing_ref)
            except PanelNotFound:
                log.info("Node plan %s vanished — recreating %r", existing_ref, spec.name)
            else:
                if remote.duration_seconds is not None and payload.duration_seconds is None:
                    # PATCH null leaves duration unchanged. A fresh template is
                    # required; existing customers retain their original terms.
                    return (await self._client.create_plan(payload)).id
                if self._plan_drifted(remote, payload):
                    await self._client.update_plan(existing_ref, payload)
                    updated = await self._client.get_plan(existing_ref)
                    if self._plan_drifted(updated, payload):
                        raise PanelValidation(default_text("error.node_response"))
                return existing_ref

        created = await self._client.create_plan(payload)
        return created.id

    @staticmethod
    def _plan_drifted(remote: Any, payload: PlanPatch) -> bool:
        checks = (
            (remote.name, payload.name),
            (remote.enabled, payload.enabled),
            (remote.start_policy, payload.start_policy),
            (remote.traffic_limit_bytes, payload.traffic_limit_bytes),
            (remote.duration_seconds, payload.duration_seconds),
            (remote.device_limit, payload.device_limit),
            (remote.speed_limit_down_kbps, payload.speed_limit_down_kbps),
            (remote.speed_limit_up_kbps, payload.speed_limit_up_kbps),
            (remote.interface_id, payload.interface_id),
        )
        return any(actual != expected for actual, expected in checks)

    # -- provisioning ------------------------------------------------------
    async def purchase(
        self,
        *,
        plan_ref: str,
        username: str,
        device_name: str,
        idempotency_key: str,
        device_count: int = 1,
    ) -> PurchaseResult:
        result = await self._client.create_purchase(
            plan_ref,
            idempotency_key=idempotency_key,
            username=username,
            device_name=device_name,
            device_count=device_count if device_count != 1 else None,
        )
        return PurchaseResult(
            user_id=result.user_id,
            device_id=result.device_id,
            device_ids=result.all_device_ids,
            username=username,
            plan_ref=result.template_id,
            operation_id=result.operation_id,
            created_at=result.created_at,
        )

    async def recover_purchase(self, idempotency_key: str) -> PurchaseResult | None:
        try:
            found = await self._client.operation_result(idempotency_key)
        except PanelNotFound as exc:
            if exc.panel_code == "OPERATION_NOT_FOUND":
                return None
            raise
        if found.kind != "purchase":
            raise PanelError(default_text("error.node_response"))
        return PurchaseResult(
            user_id=found.user_id,
            device_id=found.device_id,
            device_ids=found.all_device_ids,
            username="",
            plan_ref=found.template_id,
            operation_id=found.operation_id,
            created_at=found.created_at,
            recovered=True,
        )

    # -- users -------------------------------------------------------------
    async def get_user(self, user_ref: str) -> RemoteUser:
        return to_remote_user(await self._client.get_user(user_ref))

    async def renew_user(
        self, user_ref: str, *, duration_seconds: int | None, idempotency_key: str | None = None
    ) -> RemoteUser:
        return to_remote_user(
            await self._client.renew_user(
                user_ref,
                mode="from_expiration",
                duration_seconds=duration_seconds,
                idempotency_key=idempotency_key,
            )
        )

    async def set_limits(
        self,
        user_ref: str,
        *,
        traffic_limit_bytes: int | None = None,
        device_limit: int | None = None,
        speed_limit_down_kbps: int | None = None,
        speed_limit_up_kbps: int | None = None,
    ) -> RemoteUser:
        body: dict[str, Any] = {}
        if traffic_limit_bytes is not None:
            body["traffic_limit_bytes"] = traffic_limit_bytes
        if device_limit is not None:
            body["device_limit"] = device_limit
        if speed_limit_down_kbps is not None:
            body["speed_limit_down_kbps"] = speed_limit_down_kbps
        if speed_limit_up_kbps is not None:
            body["speed_limit_up_kbps"] = speed_limit_up_kbps
        if not body:
            return await self.get_user(user_ref)
        return to_remote_user(await self._client.update_user(user_ref, body))

    async def reset_traffic(self, user_ref: str) -> RemoteUser:
        return to_remote_user(await self._client.reset_traffic(user_ref))

    async def top_up_quota(self, user_ref: str, num_bytes: int, *, idempotency_key: str) -> RemoteUser:
        try:
            result = await self._client.top_up_quota(user_ref, num_bytes, idempotency_key=idempotency_key)
        except PanelUnavailable:
            result = await self._client.operation_result(idempotency_key)
        if result.kind != "quota_top_up" or result.user_id != user_ref:
            raise PanelError(default_text("error.node_response"))
        return await self.get_user(user_ref)

    async def set_enabled(self, user_ref: str, enabled: bool) -> RemoteUser:
        if enabled:
            return to_remote_user(await self._client.enable_user(user_ref))
        return to_remote_user(await self._client.disable_user(user_ref))

    # -- devices -----------------------------------------------------------
    async def list_devices(self, user_ref: str) -> list[RemoteDevice]:
        return [to_remote_device(device) for device in await self._client.list_devices(user_ref)]

    async def create_device(self, user_ref: str, *, name: str) -> RemoteDevice:
        return to_remote_device(await self._client.create_device(user_ref, name=name))

    async def delete_device(self, device_ref: str) -> None:
        await self._client.delete_device(device_ref)

    async def regenerate_device(self, device_ref: str) -> RemoteDevice:
        return to_remote_device(await self._client.regenerate_device(device_ref))

    async def device_config(self, device_ref: str) -> str:
        return await self._client.device_config(device_ref)

    async def device_qr(self, device_ref: str) -> bytes | None:
        try:
            return await self._client.device_qr(device_ref)
        except PanelError as exc:  # a QR failure must never block delivery
            log.warning("Node QR failed for device %s: %s", device_ref, exc.message)
            return None

    # -- subscription ------------------------------------------------------
    async def subscription_link(self, user_ref: str) -> SubscriptionLink:
        link = await self._client.customer_subscription(user_ref)
        return SubscriptionLink(path=link.path)

    async def rotate_subscription(self, user_ref: str) -> SubscriptionLink:
        rotated = await self._client.rotate_customer_access(user_ref)
        return SubscriptionLink(path=rotated.path, devices_rotated=rotated.devices_rotated)

    # -- optional ----------------------------------------------------------
    async def queue_next_plan(
        self,
        user_ref: str,
        plan_ref: str,
        *,
        carry_unused_traffic: bool = False,
        idempotency_key: str | None = None,
    ) -> bool:
        await self._client.put_next_plan(
            user_ref,
            plan_ref,
            carry_unused_traffic=carry_unused_traffic,
            idempotency_key=idempotency_key,
        )
        return True

    async def next_plan(self, user_ref: str) -> QueuedPlan | None:
        queued = await self._client.get_next_plan(user_ref)
        return QueuedPlan(queued.template_id, queued.state, queued.created_at) if queued else None

    async def plan_activations(self, user_ref: str) -> list[PlanActivation]:
        return [
            PlanActivation(item.activation_id, item.template_id, item.activated_at)
            for item in await self._client.list_next_plan_activations(user_ref, limit=100)
        ]

    async def clear_next_plan(self, user_ref: str) -> bool:
        await self._client.delete_next_plan(user_ref)
        return True


__all__ = ["STATUS_MAP", "WGGuardProvider", "normalise_status", "to_remote_device", "to_remote_user"]
