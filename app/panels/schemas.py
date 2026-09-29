"""Typed mirrors of the WG-Guard OpenAPI schemas.

The upstream document is an additive-only V1 contract, so every model allows
unknown fields (``extra="allow"``) — a newer node never breaks an older bot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

UserStatus = Literal[
    "active",
    "disabled",
    "suspended",
    "expired",
    "traffic_exceeded",
    "waiting_first_connection",
]
StartPolicy = Literal["immediate", "first_connection"]
BackendMode = Literal["kernel", "userspace"]
InterfacePreset = Literal[
    "plain", "recommended", "performance", "balanced", "resilient", "suggested", "randomized", "custom"
]
WebhookEvent = Literal[
    "user.created",
    "user.updated",
    "user.enabled",
    "user.disabled",
    "user.expired",
    "user.traffic_exceeded",
    "user.first_connected",
    "device.created",
    "device.deleted",
    "node.started",
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
class Status(_Model):
    status: str = ""


class NodeInterface(_Model):
    id: str
    name: str = ""
    listen_port: int | None = None
    ipv4_subnet: str | None = None
    enabled: bool = True
    backend_mode: str | None = None
    devices: int | None = None


class Node(_Model):
    version: str | None = None
    node_id: str | None = None
    tools_version: str | None = None
    uptime_seconds: int | None = None
    interfaces: list[NodeInterface] = Field(default_factory=list)


class TelemetryPoint(_Model):
    timestamp: datetime
    health: Literal["healthy", "degraded", "unavailable"]
    cpu_percent: float | None = None
    memory_percent: float | None = None
    disk_percent: float | None = None
    load_1: float | None = None
    uptime_seconds: int | None = None
    online_users: int | None = None
    active_peers: int | None = None
    enabled_interfaces: int | None = None
    vpn_rx_bytes_per_second: float | None = None
    vpn_tx_bytes_per_second: float | None = None


class Telemetry(_Model):
    cadence_seconds: int = 30
    latest: TelemetryPoint | None = None
    points: list[TelemetryPoint] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Users & devices
# ---------------------------------------------------------------------------
class User(_Model):
    id: str
    username: str = ""
    display_name: str | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    status: UserStatus = "active"
    disable_reason: str | None = None
    traffic_limit_bytes: int | None = None
    traffic_used_rx: int = 0
    traffic_used_tx: int = 0
    traffic_used_total: int = 0
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    device_limit: int | None = None
    plan_id: str | None = None
    interface_id: str | None = None
    start_policy: StartPolicy = "immediate"
    duration_seconds: int | None = None
    activated_at: datetime | None = None
    expires_at: datetime | None = None
    last_activity_at: datetime | None = None
    enabled: bool = True
    deleted: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_usable(self) -> bool:
        return self.enabled and self.status in ("active", "waiting_first_connection")


class UserPage(_Model):
    items: list[User] = Field(default_factory=list)
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return bool(self.next_cursor)


class Device(_Model):
    id: str
    user_id: str
    interface_id: str | None = None
    name: str = ""
    ipv4_address: str | None = None
    public_key: str | None = None
    enabled: bool = True
    last_handshake_at: datetime | None = None
    last_endpoint: str | None = None
    rx_bytes: int = 0
    tx_bytes: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class UserCreate(_Model):
    username: str
    display_name: str | None = None
    note: str | None = None
    tags: list[str] | None = None
    traffic_limit_bytes: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    device_limit: int | None = None
    plan_id: str | None = None
    interface_id: str | None = None
    start_policy: StartPolicy | None = None
    duration_seconds: int | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class UserPatch(_Model):
    display_name: str | None = None
    note: str | None = None
    tags: list[str] | None = None
    traffic_limit_bytes: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    device_limit: int | None = None
    plan_id: str | None = None
    interface_id: str | None = None
    duration_seconds: int | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Plans & interfaces
# ---------------------------------------------------------------------------
class Plan(_Model):
    id: str
    name: str = ""
    traffic_limit_bytes: int | None = None
    duration_seconds: int | None = None
    start_policy: StartPolicy = "immediate"
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    interface_id: str | None = None
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PlanPatch(_Model):
    name: str
    traffic_limit_bytes: int | None = None
    duration_seconds: int | None = None
    start_policy: StartPolicy | None = None
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    interface_id: str | None = None
    enabled: bool | None = None


class Obfuscation(_Model):
    enabled: bool | None = None
    jc: int | None = None
    jmin: int | None = None
    jmax: int | None = None
    s1: int | None = None
    s2: int | None = None
    s3: int | None = None
    s4: int | None = None
    h1: int | str | None = None
    h2: int | str | None = None
    h3: int | str | None = None
    h4: int | str | None = None
    i1: str | None = None
    i2: str | None = None
    i3: str | None = None
    i4: str | None = None
    i5: str | None = None
    header_protection_key_set: bool | None = None
    content_padding_addition: int | str | None = None
    rekey_after_time: int | str | None = None
    rekey_timeout: int | str | None = None
    reject_after_time: int | str | None = None
    keepalive_timeout: int | str | None = None
    max_handshake_attempts: int | str | None = None
    random_trailers: bool | None = None
    disable_cookies: bool | None = None


class Interface(_Model):
    id: str
    name: str = ""
    listen_port: int | None = None
    ipv4_subnet: str | None = None
    mtu: int | None = None
    public_key: str | None = None
    obfuscation: Obfuscation | None = None
    preset: InterfacePreset | None = None
    enabled: bool = True
    backend_mode: BackendMode | None = None
    endpoint_override: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------
class PurchaseRequest(_Model):
    plan_id: str
    username: str | None = None
    device_name: str = "device-1"


class OperationResult(_Model):
    operation_id: str
    kind: Literal["purchase"] = "purchase"
    state: Literal["committed"] = "committed"
    user_id: str
    device_id: str
    plan_id: str
    created_at: datetime | None = None


class CustomerLink(_Model):
    path: str


class CustomerLinkRotation(_Model):
    path: str
    devices_rotated: int = 0


class NextPlanTerms(_Model):
    name: str
    traffic_limit_bytes: int | None = None
    duration_seconds: int | None = None
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    interface_id: str | None = None


class NextPlan(_Model):
    user_id: str
    plan_id: str
    terms: NextPlanTerms
    carry_unused_traffic: bool = False
    state: Literal["queued", "needs_review"] = "queued"
    review_reason: str | None = None
    created_at: datetime | None = None


class NextPlanActivation(_Model):
    activation_id: str
    user_id: str
    plan_id: str
    trigger: Literal["time", "quota"]
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    activated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Webhooks
# ---------------------------------------------------------------------------
class WebhookEndpoint(_Model):
    id: str
    reseller_id: str | None = None
    url: str = ""
    enabled: bool = True
    events: list[str] = Field(default_factory=list)
    secret: str | None = None
    created_at: datetime | None = None
    stats: dict[str, int] | None = None


class WebhookReceipt(_Model):
    id: str
    event_id: str
    event_type: str
    status: Literal["pending", "delivered", "dead"]
    attempts: int = 0
    next_attempt_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WebhookEnvelope(_Model):
    id: str
    type: str
    timestamp: datetime | None = None
    node_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class ListResponse(_Model):
    """Generic ``{items: [...]}`` envelope used by several endpoints."""

    items: list[Any] = Field(default_factory=list)


__all__ = [
    "BackendMode",
    "CustomerLink",
    "CustomerLinkRotation",
    "Device",
    "Interface",
    "InterfacePreset",
    "ListResponse",
    "NextPlan",
    "NextPlanActivation",
    "NextPlanTerms",
    "Node",
    "NodeInterface",
    "Obfuscation",
    "OperationResult",
    "Plan",
    "PlanPatch",
    "PurchaseRequest",
    "StartPolicy",
    "Status",
    "Telemetry",
    "TelemetryPoint",
    "User",
    "UserCreate",
    "UserPage",
    "UserPatch",
    "UserStatus",
    "WebhookEndpoint",
    "WebhookEnvelope",
    "WebhookEvent",
    "WebhookReceipt",
]
