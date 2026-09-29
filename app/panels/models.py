"""Canonical, panel-agnostic models.

Every VPN backend (WG-Guard today, PasarGuard/Marzban/x-ui tomorrow) is adapted
into these shapes.  The rest of the application — orders, provisioning,
delivery, the admin panel — speaks **only** this vocabulary, so adding a backend
never means touching business logic.

Rules for these models:

* They are frozen: an adapter returns a new value, it never mutates shared
  state.
* They carry no HTTP, no ORM and no vendor field names in their public API.
  Anything vendor-specific that we still need is kept in ``raw`` so it can be
  logged or displayed without leaking into the domain.
* Times are timezone-aware UTC, matching :mod:`app.core.jalali`.
* Traffic is bytes, duration is seconds, speed is kbit/s.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

#: Canonical lifecycle states.  Every adapter maps its vendor statuses onto
#: these; the domain never compares vendor strings.
RemoteStatus = Literal[
    "active",
    "waiting_first_connection",
    "disabled",
    "expired",
    "traffic_exceeded",
]

StartPolicy = Literal["immediate", "first_connection"]


@dataclass(frozen=True, slots=True)
class ProviderHealth:
    """Result of probing a node."""

    online: bool
    degraded: bool = False
    version: str | None = None
    node_id: str | None = None
    uptime_seconds: int | None = None
    interface_count: int = 0
    message: str | None = None

    @property
    def state(self) -> Literal["online", "degraded", "offline"]:
        if not self.online:
            return "offline"
        return "degraded" if self.degraded else "online"


@dataclass(frozen=True, slots=True)
class RemoteInterface:
    """A tunnel interface / profile on the node."""

    id: str
    name: str = ""
    enabled: bool = True
    listen_port: int | None = None
    ipv4_subnet: str | None = None
    public_key: str | None = None
    devices: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NodeInfo:
    provider_kind: str
    name: str = ""
    version: str | None = None
    node_id: str | None = None
    uptime_seconds: int | None = None
    interfaces: tuple[RemoteInterface, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlanSpec:
    """The technical terms we want a node-side plan to have.

    Pricing is deliberately absent: money never leaves this application.
    """

    name: str
    traffic_limit_bytes: int | None = None
    duration_seconds: int | None = None
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    start_policy: StartPolicy = "first_connection"
    interface_ref: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RemoteUser:
    """A provisioned customer account on a node."""

    id: str
    username: str
    status: RemoteStatus = "active"
    enabled: bool = True
    traffic_limit_bytes: int | None = None
    traffic_used_bytes: int = 0
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    expires_at: datetime | None = None
    activated_at: datetime | None = None
    last_activity_at: datetime | None = None
    plan_ref: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def remaining_bytes(self) -> int | None:
        if self.traffic_limit_bytes is None:
            return None
        return max(self.traffic_limit_bytes - self.traffic_used_bytes, 0)

    @property
    def is_usable(self) -> bool:
        return self.enabled and self.status in ("active", "waiting_first_connection")


@dataclass(frozen=True, slots=True)
class RemoteDevice:
    """One client peer belonging to a user."""

    id: str
    user_id: str
    name: str = ""
    ipv4_address: str | None = None
    enabled: bool = True
    public_key: str | None = None
    last_handshake_at: datetime | None = None
    rx_bytes: int = 0
    tx_bytes: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PurchaseResult:
    """Outcome of an atomic provisioning call.

    ``recovered`` is ``True`` when the result was read back after an ambiguous
    transport failure rather than observed directly — the caller treats both the
    same, but it is useful in logs and tests.
    """

    user_id: str
    device_id: str
    username: str
    plan_ref: str | None = None
    operation_id: str | None = None
    created_at: datetime | None = None
    recovered: bool = False


@dataclass(frozen=True, slots=True)
class SubscriptionLink:
    """A customer's private subscription capability (a secret)."""

    path: str
    devices_rotated: int = 0


__all__ = [
    "NodeInfo",
    "PlanSpec",
    "ProviderHealth",
    "PurchaseResult",
    "RemoteDevice",
    "RemoteInterface",
    "RemoteStatus",
    "RemoteUser",
    "StartPolicy",
    "SubscriptionLink",
]
