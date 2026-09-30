"""Typed mirrors of the WG-Guard OpenAPI schemas.

Response schemas tolerate additive V1 fields (``extra="allow"``). Request
schemas use the current strict contract and preserve explicit null limits.

Two more tolerance rules follow from the same contract, and both have bitten
this deployment in production:

* **"Not set" arrives as ``null``.**  The node serialises an empty ``tags``
  list (or a missing ``username``) as JSON ``null``, which a strict field
  rejects — and a rejected read-back used to abort a *committed* purchase.  The
  shared :meth:`_Model._null_means_default` validator therefore turns ``null``
  into the field's default for every model here, once.
* **A state name may be one this build has never heard of.**  Response fields
  such as ``status``, ``start_policy``, ``preset`` and ``backend_mode`` are
  plain ``str``: the canonical mapping happens in the provider
  (``normalise_status``), not in validation.  Request payloads keep their
  ``Literal`` types, because there *we* choose the value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Vendor statuses this build understands.  A node may introduce more, and an
#: unknown name must never fail a read-back, so :attr:`User.status` is typed as
#: ``str`` and mapped by ``normalise_status`` instead.
UserStatus = Literal[
    "active",
    "disabled",
    "suspended",
    "expired",
    "traffic_exceeded",
    "waiting_first_connection",
]
StartPolicy = Literal["immediate", "first_connection"]
#: Vocabulary the node documents.  Response fields stay ``str`` on purpose (see
#: the module docstring): these aliases exist so request payloads, and anyone
#: reading the code, see the exact accepted spellings.
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

    @model_validator(mode="before")
    @classmethod
    def _null_means_default(cls, data: Any) -> Any:
        """Treat an explicit ``null`` as "the node did not set this field".

        One rule for the whole additive contract: an empty ``tags`` list or a
        missing ``username`` comes over the wire as ``null``, and every model
        here declares a sensible default for exactly that case.  Required
        fields keep ``null`` (and therefore still fail loudly, as they should).
        """
        if not isinstance(data, dict):
            return data
        fields = cls.model_fields
        return {
            key: value
            for key, value in data.items()
            if not (value is None and key in fields and not fields[key].is_required())
        }


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------
class _RequestModel(BaseModel):
    """Requests are strict and preserve explicit nulls for tri-state PATCH."""

    model_config = ConfigDict(extra="forbid")


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
    #: Vendor status.  Unknown values are mapped by ``normalise_status`` rather
    #: than rejected here — see the module docstring.
    status: str = "active"
    disable_reason: str | None = None
    traffic_limit_bytes: int | None = None
    traffic_used_rx: int = 0
    traffic_used_tx: int = 0
    traffic_used_total: int = 0
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    device_limit: int | None = None
    template_id: str | None = None
    interface_id: str | None = None
    start_policy: str = "immediate"
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


class _LimitsRequest(_RequestModel):
    traffic_limit_bytes: int | None = Field(default=None, ge=0, strict=True)
    speed_limit_down_kbps: int | None = Field(default=None, ge=1, strict=True)
    speed_limit_up_kbps: int | None = Field(default=None, ge=1, strict=True)
    device_limit: int | None = Field(default=None, ge=1, strict=True)
    duration_seconds: int | None = Field(default=None, ge=1, strict=True)


class UserCreate(_LimitsRequest):
    username: str
    display_name: str | None = None
    note: str | None = None
    tags: list[str] | None = None
    template_id: str | None = None
    interface_id: str | None = None
    start_policy: StartPolicy | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class UserPatch(_LimitsRequest):
    display_name: str | None = None
    note: str | None = None
    tags: list[str] | None = None
    template_id: str | None = None
    interface_id: str | None = None
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
    start_policy: str = "immediate"
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    interface_id: str | None = None
    enabled: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PlanPatch(_LimitsRequest):
    name: str | None = None
    start_policy: StartPolicy | None = None
    interface_id: str | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value.encode("utf-8")) > 64):
            raise ValueError("Template name must be non-blank and at most 64 UTF-8 bytes")
        return value


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
    preset: str | None = None
    enabled: bool = True
    backend_mode: str | None = None
    endpoint_override: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------
class PurchaseRequest(_RequestModel):
    template_id: str
    username: str | None = None
    device_name: str = "device-1"
    device_count: int | None = Field(default=None, ge=1, le=100, strict=True)


class OperationResult(_Model):
    operation_id: str
    kind: Literal["purchase"] = "purchase"
    state: Literal["committed"] = "committed"
    user_id: str
    device_id: str
    device_ids: list[str] | None = None
    template_id: str | None = None
    created_at: datetime | None = None

    @model_validator(mode="after")
    def _device_identity(self) -> OperationResult:
        if self.device_ids is not None and (
            not 1 <= len(self.device_ids) <= 100
            or self.device_ids[0] != self.device_id
            or len(set(self.device_ids)) != len(self.device_ids)
        ):
            raise ValueError("Inconsistent purchase device identities")
        return self

    @property
    def all_device_ids(self) -> tuple[str, ...]:
        return tuple(self.device_ids) if self.device_ids is not None else (self.device_id,)


class QuotaSnapshot(_Model):
    traffic_limit_bytes: int
    traffic_used_rx: int = 0
    traffic_used_tx: int = 0
    traffic_used_total: int = 0
    enabled: bool = True
    disable_reason: str | None = None


class QuotaTopUpResult(_Model):
    operation_id: str
    kind: Literal["quota_top_up"]
    state: Literal["committed"]
    user_id: str
    before: QuotaSnapshot
    after: QuotaSnapshot
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
    template_id: str
    terms: NextPlanTerms
    carry_unused_traffic: bool = False
    state: Literal["queued", "needs_review"] = "queued"
    review_reason: str | None = None
    created_at: datetime | None = None


class NextPlanActivation(_Model):
    activation_id: str
    user_id: str
    template_id: str
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
    "QuotaSnapshot",
    "QuotaTopUpResult",
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
