"""In-memory state store for the mock WG-Guard panel.

Every mutable collection lives here behind a single :class:`asyncio.Lock`; the
FastAPI layer (``app.py``) only orchestrates locking, validation and
serialisation.  Field names follow the upstream OpenAPI 3.2.1 document in
``docs/upstream-api/openapi-wg-guard.json`` exactly.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

SCOPE_ALL = "*"

#: Every scope referenced by the upstream contract.
ALL_SCOPES: frozenset[str] = frozenset(
    {
        "node.read",
        "node.settings",
        "stats.read",
        "users.read",
        "users.create",
        "users.update",
        "users.delete",
        "users.bulk",
        "devices.read",
        "devices.write",
        "configs.read",
        "traffic.read",
        "traffic.update",
        "plans.read",
        "plans.write",
        "interfaces.read",
        "interfaces.write",
        "webhooks.read",
        "webhooks.write",
        "purchases.create",
        "operations.read",
        "subscriptions.read",
        "subscriptions.rotate",
        "next_plans.read",
        "next_plans.write",
    }
)

#: Convenience token scope set used for the seeded read-only test token.
READ_ONLY_SCOPES: frozenset[str] = frozenset(
    {
        "node.read",
        "stats.read",
        "users.read",
        "devices.read",
        "configs.read",
        "traffic.read",
        "plans.read",
        "interfaces.read",
        "webhooks.read",
        "operations.read",
        "subscriptions.read",
        "next_plans.read",
    }
)

DEFAULT_TOKEN = "wg_test_token"
READONLY_TOKEN = "wg_readonly_token"

#: Integration results are retained for 90 days by the real panel.
IDEMPOTENCY_TTL_SECONDS = 90 * 24 * 3600
#: Cap on the ``/__mock__/requests`` journal.
MAX_LOGGED_REQUESTS = 5000

USER_FIELDS: tuple[str, ...] = (
    "id",
    "username",
    "display_name",
    "note",
    "tags",
    "status",
    "disable_reason",
    "traffic_limit_bytes",
    "traffic_used_rx",
    "traffic_used_tx",
    "traffic_used_total",
    "speed_limit_down_kbps",
    "speed_limit_up_kbps",
    "device_limit",
    "plan_id",
    "interface_id",
    "start_policy",
    "duration_seconds",
    "activated_at",
    "expires_at",
    "last_activity_at",
    "enabled",
    "metadata",
    "deleted",
    "created_at",
    "updated_at",
)

DEVICE_FIELDS: tuple[str, ...] = (
    "id",
    "user_id",
    "interface_id",
    "name",
    "ipv4_address",
    "public_key",
    "enabled",
    "last_handshake_at",
    "last_endpoint",
    "rx_bytes",
    "tx_bytes",
    "created_at",
    "updated_at",
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def utc_now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    """Render a datetime as an RFC 3339 UTC string (``...Z``, microsecond precision).

    Microseconds are always emitted so that lexicographic string ordering matches
    chronological ordering for every timestamp this mock produces.
    """
    if value is None:
        return None
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str) -> datetime:
    """Parse an RFC 3339 timestamp produced by :func:`iso`."""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def new_id(prefix: str, size: int = 12) -> str:
    """Return a stable-looking opaque identifier such as ``usr_1a2b...``."""
    return f"{prefix}_{secrets.token_hex(size // 2)}"


def new_wg_key() -> str:
    """Return a realistic 44-character base64 WireGuard key."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def fingerprint(payload: Any) -> str:
    """Return a canonical SHA-256 fingerprint of a JSON-serialisable payload."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def encode_cursor(offset: int) -> str:
    """Encode a list offset as an opaque URL-safe cursor."""
    raw = json.dumps({"o": offset}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> int:
    """Decode a cursor produced by :func:`encode_cursor`.

    Raises:
        ValueError: when the cursor is not a cursor produced by this mock.
    """
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        offset = int(data["o"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:  # pragma: no cover - defensive
        raise ValueError("malformed cursor") from exc
    if offset < 0:
        raise ValueError("malformed cursor")
    return offset


def valid_idempotency_key(key: str | None) -> bool:
    """Return True when *key* is 1-128 printable ASCII characters without spaces."""
    if not key or len(key) > 128:
        return False
    return all(0x21 <= ord(char) <= 0x7E for char in key)


def derived_username(key: str) -> str:
    """Derive a stable username from an idempotency key (matches the contract's pattern)."""
    return "u_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


def next_ipv4(subnet: str, used: set[str]) -> str | None:
    """Return the next free host address of a /24-style subnet, or None if exhausted."""
    head = subnet.split("/")[0].split(".")
    if len(head) != 4:  # pragma: no cover - defensive
        return None
    for host in range(2, 255):
        candidate = f"{head[0]}.{head[1]}.{head[2]}.{host}"
        if candidate not in used:
            return candidate
    return None


def subnet_dns(subnet: str) -> str:
    """Return the in-tunnel DNS address of a subnet."""
    head = subnet.split("/")[0].split(".")
    return ".".join([*head[:3], "1"]) if len(head) == 4 else "1.1.1.1"


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
DEFAULT_PLAN_SEED: tuple[dict[str, Any], ...] = (
    {
        "id": "plan_starter",
        "name": "Starter",
        "traffic_limit_bytes": 32 * 1024**3,
        "duration_seconds": 30 * 24 * 3600,
        "device_limit": 2,
        "speed_limit_down_kbps": 51_200,
        "speed_limit_up_kbps": 25_600,
    },
    {
        "id": "plan_standard",
        "name": "Standard",
        "traffic_limit_bytes": 100 * 1024**3,
        "duration_seconds": 30 * 24 * 3600,
        "device_limit": 3,
        "speed_limit_down_kbps": 102_400,
        "speed_limit_up_kbps": 51_200,
    },
    {
        "id": "plan_unlimited",
        "name": "Unlimited",
        "traffic_limit_bytes": None,
        "duration_seconds": 30 * 24 * 3600,
        "device_limit": 5,
        "speed_limit_down_kbps": None,
        "speed_limit_up_kbps": None,
    },
    {
        "id": "plan_retired",
        "name": "Retired",
        "traffic_limit_bytes": 10 * 1024**3,
        "duration_seconds": 30 * 24 * 3600,
        "device_limit": 1,
        "enabled": False,
    },
)

DEFAULT_OBFUSCATION: dict[str, Any] = {
    "enabled": True,
    "jc": 5,
    "jmin": 40,
    "jmax": 70,
    "s1": 86,
    "s2": 61,
    "h1": "100-110",
    "h2": 200,
    "h3": "300-310",
    "h4": 400,
}

DEFAULT_INTERFACE_SEED: tuple[dict[str, Any], ...] = (
    {
        "id": "iface_awg0",
        "name": "awg0",
        "listen_port": 51820,
        "ipv4_subnet": "10.8.0.0/24",
        "mtu": 1420,
        "preset": "recommended",
        "backend_mode": "kernel",
        "endpoint_override": "vpn.mock.local:51820",
        "obfuscation": dict(DEFAULT_OBFUSCATION),
    },
    {
        "id": "iface_awg1",
        "name": "awg1",
        "listen_port": 51821,
        "ipv4_subnet": "10.9.0.0/24",
        "mtu": 1380,
        "preset": "balanced",
        "backend_mode": "userspace",
        "obfuscation": {**DEFAULT_OBFUSCATION, "jc": 4, "s1": 40, "s2": 90},
    },
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "node.endpoint": "vpn.mock.local",
    "node.dns": "1.1.1.1",
    "node.hostname": "mock-node",
    "api.rate_limit_per_minute": 600,
    "webhooks.max_attempts": 12,
    "downloads.filename_prefix": "",
    "downloads.filename_suffix": "",
    "downloads.profile_name": "WG-Guard",
}


@dataclass(slots=True)
class MockConfig:
    """Typed configuration for :func:`mock_wg_panel.app.create_app`.

    Attributes:
        tokens: mapping of API token to the scopes it carries. ``"*"`` grants all.
        plans: partial plan documents used to seed the catalog.
        interfaces: partial interface documents used to seed the inventory.
        clock_offset_seconds: added to the wall clock (negative travels to the past).
        node_id: node identity reported by ``GET /api/v1/node``.
        version: node/panel version string.
        settings: seeded settings registry.
        seed_users: full or partial user documents created at reset time.
        ready: value reported by ``GET /readyz`` (``503`` when False).
    """

    tokens: dict[str, frozenset[str]] = field(
        default_factory=lambda: {
            DEFAULT_TOKEN: frozenset({SCOPE_ALL}),
            READONLY_TOKEN: READ_ONLY_SCOPES,
        }
    )
    plans: tuple[dict[str, Any], ...] = DEFAULT_PLAN_SEED
    interfaces: tuple[dict[str, Any], ...] = DEFAULT_INTERFACE_SEED
    clock_offset_seconds: float = 0.0
    node_id: str = "mock-node-1"
    version: str = "1.0.0-mock"
    settings: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    seed_users: tuple[dict[str, Any], ...] = ()
    ready: bool = True

    @classmethod
    def default(cls) -> MockConfig:
        """Return the default mock configuration."""
        return cls()

    def with_clock_offset(self, seconds: float) -> MockConfig:
        """Return a copy of this config with a different clock offset."""
        return MockConfig(
            tokens=dict(self.tokens),
            plans=self.plans,
            interfaces=self.interfaces,
            clock_offset_seconds=seconds,
            node_id=self.node_id,
            version=self.version,
            settings=dict(self.settings),
            seed_users=self.seed_users,
            ready=self.ready,
        )


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class IdempotencyRecord:
    """A replayed response body for one ``Idempotency-Key``."""

    key: str
    fingerprint: str
    status_code: int
    body: Any
    headers: dict[str, str] = field(default_factory=dict)
    operation_id: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime = field(default_factory=lambda: utc_now() + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS))

    def expired(self, now: datetime) -> bool:
        """Return True when the record fell out of the 90-day retention window."""
        return now >= self.expires_at


@dataclass(slots=True)
class FailureRule:
    """Test-affordance rule: fail the next N matching requests."""

    path: str
    status_code: int = 503
    body: dict[str, Any] | None = None
    remaining: int | None = 1  # None = every matching request until reset


@dataclass(slots=True)
class SlowRule:
    """Test-affordance rule: delay the next N matching requests."""

    path: str
    seconds: float = 1.0
    remaining: int | None = 1


# --------------------------------------------------------------------------- #
# serialisation helpers
# --------------------------------------------------------------------------- #
def refresh_user(user: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Re-derive ``status``/``enabled``/``disable_reason`` from the clock and counters."""
    if user.get("deleted"):
        user.update(status="disabled", enabled=False)
        return user
    limit = user.get("traffic_limit_bytes")
    used = int(user.get("traffic_used_rx", 0)) + int(user.get("traffic_used_tx", 0))
    if limit is not None and used >= int(limit):
        user.update(status="traffic_exceeded", enabled=False, disable_reason="traffic_limit")
        return user
    if not user.get("enabled", True):
        reason = user.get("disable_reason") or "manual"
        if reason not in ("manual", "admin_action", "expired", "traffic_limit"):
            reason = "manual"
        user.update(disable_reason=reason, status="expired" if reason == "expired" else "disabled")
        return user
    expires_at = user.get("expires_at")
    if expires_at and parse_iso(expires_at) <= now:
        user.update(status="expired", enabled=False, disable_reason="expired")
        return user
    user["disable_reason"] = None
    user["status"] = "active" if user.get("activated_at") else "waiting_first_connection"
    return user


def serialize_user(user: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Return the wire representation of a user (never leaks ``sub_token``)."""
    refresh_user(user, now)
    user["traffic_used_total"] = int(user.get("traffic_used_rx", 0)) + int(user.get("traffic_used_tx", 0))
    return {name: user.get(name) for name in USER_FIELDS}


def serialize_device(device: dict[str, Any]) -> dict[str, Any]:
    """Return the wire representation of a device (never leaks private keys)."""
    return {name: device.get(name) for name in DEVICE_FIELDS}


def build_plan(payload: dict[str, Any], *, plan_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Build a complete plan document from a partial payload."""
    stamp = iso(now or utc_now())
    return {
        "id": plan_id or payload.get("id") or new_id("plan"),
        "name": payload.get("name") or "unnamed",
        "traffic_limit_bytes": payload.get("traffic_limit_bytes"),
        "duration_seconds": payload.get("duration_seconds", 30 * 24 * 3600),
        "start_policy": payload.get("start_policy", "immediate"),
        "device_limit": payload.get("device_limit"),
        "speed_limit_down_kbps": payload.get("speed_limit_down_kbps"),
        "speed_limit_up_kbps": payload.get("speed_limit_up_kbps"),
        "interface_id": payload.get("interface_id"),
        "enabled": bool(payload.get("enabled", True)),
        "created_at": payload.get("created_at", stamp),
        "updated_at": stamp,
    }


def build_interface(
    payload: dict[str, Any],
    *,
    interface_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a complete interface document from a partial payload."""
    stamp = iso(now or utc_now())
    obfuscation = payload.get("obfuscation")
    if obfuscation is not None:
        obfuscation = {**DEFAULT_OBFUSCATION, **obfuscation}
    return {
        "id": interface_id or payload.get("id") or new_id("iface"),
        "name": payload.get("name") or "awg0",
        "listen_port": int(payload.get("listen_port") or 51820),
        "ipv4_subnet": payload.get("ipv4_subnet") or "10.8.0.0/24",
        "mtu": int(payload.get("mtu") or 1420),
        "public_key": payload.get("public_key") or new_wg_key(),
        "obfuscation": obfuscation if obfuscation is not None else dict(DEFAULT_OBFUSCATION),
        "preset": payload.get("preset") or ("custom" if payload.get("obfuscation") else "recommended"),
        "enabled": bool(payload.get("enabled", True)),
        "backend_mode": payload.get("backend_mode") or "kernel",
        "endpoint_override": payload.get("endpoint_override")
        or f"vpn.mock.local:{int(payload.get('listen_port') or 51820)}",
        "created_at": payload.get("created_at", stamp),
        "updated_at": stamp,
    }


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class MockStore:
    """All mutable mock state, guarded by a single :class:`asyncio.Lock`."""

    def __init__(self, config: MockConfig | None = None) -> None:
        self.config = config or MockConfig.default()
        self.lock = asyncio.Lock()
        self.started_monotonic = time.monotonic()
        self.users: dict[str, dict[str, Any]] = {}
        self.devices: dict[str, dict[str, Any]] = {}
        self.device_secrets: dict[str, dict[str, str]] = {}
        self.plans: dict[str, dict[str, Any]] = {}
        self.interfaces: dict[str, dict[str, Any]] = {}
        self.webhooks: dict[str, dict[str, Any]] = {}
        self.webhook_secrets: dict[str, str] = {}
        self.deliveries: dict[str, list[dict[str, Any]]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[str, IdempotencyRecord] = {}
        self.settings: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []
        self.failures: list[FailureRule] = []
        self.slows: list[SlowRule] = []
        self.reset()

    # -- clock ------------------------------------------------------------- #
    def now(self) -> datetime:
        """Current mock time (wall clock plus the configured offset)."""
        return utc_now() + timedelta(seconds=self.config.clock_offset_seconds)

    def now_iso(self) -> str:
        """Current mock time as an RFC 3339 string."""
        return iso(self.now()) or ""

    def uptime_seconds(self) -> int:
        """Seconds since the store was constructed."""
        return int(time.monotonic() - self.started_monotonic)

    # -- lifecycle --------------------------------------------------------- #
    def reset(self) -> None:
        """Wipe every collection back to the configured seed."""
        self.users.clear()
        self.devices.clear()
        self.device_secrets.clear()
        self.plans.clear()
        self.interfaces.clear()
        self.webhooks.clear()
        self.webhook_secrets.clear()
        self.deliveries.clear()
        self.operations.clear()
        self.idempotency.clear()
        self.requests.clear()
        self.failures.clear()
        self.slows.clear()
        self.settings = dict(self.config.settings)
        now = self.now()
        for partial in self.config.interfaces:
            iface = build_interface(dict(partial), now=now)
            self.interfaces[iface["id"]] = iface
        for partial in self.config.plans:
            plan = build_plan(dict(partial), now=now)
            self.plans[plan["id"]] = plan
        self.started_monotonic = time.monotonic()

    # -- request journal and fault injection -------------------------------- #
    def record_request(self, entry: dict[str, Any]) -> None:
        """Append one request to ``/__mock__/requests`` (bounded journal)."""
        self.requests.append(entry)
        if len(self.requests) > MAX_LOGGED_REQUESTS:
            del self.requests[: len(self.requests) - MAX_LOGGED_REQUESTS]

    @staticmethod
    def path_matches(rule_path: str, path: str) -> bool:
        """True when a rule path covers the request path (exact or sub-path)."""
        return path == rule_path or path.startswith(rule_path.rstrip("/") + "/")

    def take_failure(self, path: str) -> FailureRule | None:
        """Return the next failure rule matching *path* and consume one use."""
        for rule in list(self.failures):
            if not self.path_matches(rule.path, path):
                continue
            if rule.remaining is not None:
                rule.remaining -= 1
                if rule.remaining <= 0:
                    self.failures.remove(rule)
            return rule
        return None

    def take_delay(self, path: str) -> float:
        """Return the delay (seconds) for *path* and consume one use."""
        for rule in list(self.slows):
            if not self.path_matches(rule.path, path):
                continue
            if rule.remaining is not None:
                rule.remaining -= 1
                if rule.remaining <= 0:
                    self.slows.remove(rule)
            return rule.seconds
        return 0.0

    # -- lookups ----------------------------------------------------------- #
    def used_addresses(self) -> set[str]:
        """Every IPv4 address currently leased to a device."""
        return {device["ipv4_address"] for device in self.devices.values() if device.get("ipv4_address")}

    def devices_of(self, user_id: str) -> list[dict[str, Any]]:
        """Devices belonging to a user, oldest first."""
        return [d for d in self.devices.values() if d["user_id"] == user_id]

    def allocate_ipv4(self, interface_id: str) -> str | None:
        """Allocate the next free address on an interface."""
        iface = self.interfaces.get(interface_id)
        if iface is None:
            return None
        return next_ipv4(iface["ipv4_subnet"], self.used_addresses())

    def lookup_delivery(self, webhook_id: str, delivery_id: str) -> dict[str, Any] | None:
        """Find one delivery receipt of one endpoint."""
        for receipt in self.deliveries.get(webhook_id, []):
            if receipt["id"] == delivery_id:
                return receipt
        return None
