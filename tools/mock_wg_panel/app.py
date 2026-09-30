"""FastAPI application factory for the in-memory mock WG-Guard panel.

The route surface, scopes, request/response shapes and the error envelope follow
the upstream contract in ``docs/upstream-api/wg-guard-openapi.json``.  Everything
is in-memory: restarting the process restores the seed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import struct
import zlib
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Body, Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from .store import (
    DEFAULT_OBFUSCATION,
    IDEMPOTENCY_TTL_SECONDS,
    SCOPE_ALL,
    FailureRule,
    IdempotencyRecord,
    MockConfig,
    MockStore,
    SlowRule,
    build_interface,
    build_plan,
    decode_cursor,
    derived_username,
    encode_cursor,
    fingerprint,
    iso,
    new_id,
    new_wg_key,
    parse_iso,
    refresh_user,
    serialize_device,
    serialize_user,
    subnet_dns,
    valid_idempotency_key,
)

LOGGER = logging.getLogger("mock_wg_panel")

TELEMETRY_CADENCE_SECONDS = 15
TELEMETRY_MAX_POINTS = 180
WEBHOOK_EVENTS: tuple[str, ...] = (
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
)
PLAN_SORTS = ("created_at", "username", "expires_at", "used")
USER_PATCHABLE = (
    "display_name",
    "note",
    "tags",
    "traffic_limit_bytes",
    "speed_limit_down_kbps",
    "speed_limit_up_kbps",
    "device_limit",
    "template_id",
    "interface_id",
    "duration_seconds",
    "enabled",
    "metadata",
)


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #
class ApiError(Exception):
    """An error carrying one documented ``Error`` envelope code."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def _request_id(request: Request) -> str:
    """Return the per-request id assigned by the mock middleware."""
    return getattr(request.state, "request_id", None) or f"req_{secrets.token_hex(8)}"


def _envelope(code: str, message: str, request_id: str) -> dict[str, Any]:
    """Build the documented ``{"error": {...}}`` envelope."""
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _error_response(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_envelope(code, message, request_id))


def _json_response(status_code: int, content: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content, headers=headers)


# --------------------------------------------------------------------------- #
# request models
# --------------------------------------------------------------------------- #
class PurchaseRequest(BaseModel):
    """``PurchaseRequest`` (``additionalProperties: false``)."""

    model_config = ConfigDict(extra="forbid")

    template_id: str
    username: str | None = None
    device_name: str = "device-1"
    device_count: int | None = Field(default=1, ge=1, le=100, strict=True)


class UserCreate(BaseModel):
    """``UserCreate``; ``limit`` fields are tri-state (absent = unlimited)."""

    username: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,32}$")
    display_name: str | None = None
    note: str | None = None
    tags: list[str] = Field(default_factory=list)
    traffic_limit_bytes: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    device_limit: int | None = None
    template_id: str | None = None
    interface_id: str | None = None
    start_policy: Literal["immediate", "first_connection"] = "immediate"
    duration_seconds: int | None = None
    enabled: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class UserPatch(BaseModel):
    """``UserPatch``; unknown keys are surfaced so ``username`` can be rejected."""

    model_config = ConfigDict(extra="allow")

    display_name: str | None = None
    note: str | None = None
    tags: list[str] | None = None
    traffic_limit_bytes: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    device_limit: int | None = None
    template_id: str | None = None
    interface_id: str | None = None
    duration_seconds: int | None = None
    enabled: bool | None = None
    metadata: dict[str, Any] | None = None


class DisableRequest(BaseModel):
    """Body of ``POST /users/{id}/disable`` (optional)."""

    reason: Literal["manual", "admin_action"] = "manual"


class RenewRequest(BaseModel):
    """Body of ``POST /users/{id}/renew``."""

    mode: Literal["from_expiration", "from_now", "exact"]
    duration_seconds: int | None = None
    exact: str | None = None


class TrafficAdd(BaseModel):
    """Body of ``POST /users/{id}/traffic/add``."""

    rx_bytes: int = Field(default=0, ge=0)
    tx_bytes: int = Field(default=0, ge=0)


class QuotaAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bytes: int = Field(gt=0, strict=True)


class NextPlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_id: str
    carry_unused_traffic: bool = False


class TrafficSet(BaseModel):
    """Body of ``POST /users/{id}/traffic/set``; ``null`` leaves a counter untouched."""

    rx_bytes: int | None = Field(default=None, ge=0)
    tx_bytes: int | None = Field(default=None, ge=0)


class DeviceCreate(BaseModel):
    """Body of ``POST /users/{id}/devices``."""

    name: str
    interface_id: str | None = None
    preshared_key: bool = False


class DeviceRename(BaseModel):
    """Body of ``PATCH /devices/{id}``."""

    name: str


class DeviceRegenerate(BaseModel):
    """Body of ``POST /devices/{id}/regenerate`` (optional)."""

    preshared_key: bool | None = None


class PlanWrite(BaseModel):
    """``PlanPatch`` used by ``POST /plans`` (name required)."""

    name: str
    traffic_limit_bytes: int | None = None
    duration_seconds: int | None = None
    start_policy: Literal["immediate", "first_connection"] = "immediate"
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    interface_id: str | None = None
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value.encode("utf-8")) > 64):
            raise ValueError("Invalid template name")
        return value


class PlanUpdate(BaseModel):
    """Partial plan update; every field is optional (see README deviation note)."""

    name: str | None = None
    traffic_limit_bytes: int | None = None
    duration_seconds: int | None = None
    start_policy: Literal["immediate", "first_connection"] | None = None
    device_limit: int | None = None
    speed_limit_down_kbps: int | None = None
    speed_limit_up_kbps: int | None = None
    interface_id: str | None = None
    enabled: bool | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value.encode("utf-8")) > 64):
            raise ValueError("Invalid template name")
        return value


class InterfaceCreate(BaseModel):
    """Body of ``POST /interfaces``."""

    name: str = Field(pattern=r"^awg[0-9]+$")
    listen_port: int | None = None
    ipv4_subnet: str | None = None
    mtu: int | None = None
    preset: str | None = None
    backend_mode: Literal["kernel", "userspace"] | None = None
    endpoint_override: str | None = None
    obfuscation: dict[str, Any] | None = None


class InterfaceUpdate(BaseModel):
    """Body of ``PATCH /interfaces/{id}`` (name, port and pool are immutable)."""

    mtu: int | None = None
    enabled: bool | None = None
    endpoint_override: str | None = None
    obfuscation: dict[str, Any] | None = None


class WebhookCreate(BaseModel):
    """Body of ``POST /webhooks``."""

    url: str
    events: list[str] = Field(min_length=1)
    secret: str | None = None
    include_reseller_events: bool = False


class WebhookUpdate(BaseModel):
    """Body of ``PATCH /webhooks/{id}``."""

    url: str | None = None
    events: list[str] | None = None
    enabled: bool | None = None
    secret: str | None = None
    include_reseller_events: bool | None = None


class RedeliverRequest(BaseModel):
    """Body of ``POST /webhooks/{id}/redeliver``."""

    delivery_id: str


# --- test affordances (not part of the WG-Guard contract) ------------------- #
class MockFailRequest(BaseModel):
    """Arm an injected failure for the next ``count`` matching requests."""

    model_config = ConfigDict(extra="allow")

    path: str
    count: int | None = 1
    status: int = 503
    body: dict[str, Any] | None = None


class MockSlowRequest(BaseModel):
    """Arm an injected delay for the next ``count`` matching requests."""

    path: str
    seconds: float = 1.0
    count: int | None = 1


class MockSeedPlan(PlanWrite):
    """Seed one extra plan into the catalog."""

    id: str | None = None


class MockExpireRequest(BaseModel):
    """Force-expire a user by id or username."""

    user_id: str | None = None
    username: str | None = None


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #
def _store(request: Request) -> MockStore:
    """Return the store bound to the running application."""
    return request.app.state.store


def _authorize(request: Request, scope: str) -> str:
    """Validate the bearer token and its scope; returns the presented token.

    Raises:
        ApiError: 401 for a missing/unknown token, 403 for a missing scope.
    """
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        raise ApiError(401, "UNAUTHORIZED", "missing or malformed bearer token")
    scopes = _store(request).config.tokens.get(token)
    if scopes is None:
        raise ApiError(401, "UNAUTHORIZED", "unknown API token")
    if SCOPE_ALL not in scopes and scope not in scopes:
        raise ApiError(403, "FORBIDDEN", f"token lacks required scope '{scope}'")
    return token


def require(scope: str) -> Callable[[Request], Awaitable[None]]:
    """Build a dependency that enforces one documented scope."""

    async def dependency(request: Request) -> None:
        _authorize(request, scope)

    return dependency


# --------------------------------------------------------------------------- #
# idempotency helpers
# --------------------------------------------------------------------------- #
def _idempotency_key(request: Request, *, required: bool) -> str | None:
    """Read and validate the ``Idempotency-Key`` header (1-128 printable ASCII)."""
    key = request.headers.get("idempotency-key")
    if key is None:
        if required:
            raise ApiError(400, "INVALID_REQUEST", "Idempotency-Key header is required")
        return None
    if not valid_idempotency_key(key):
        raise ApiError(
            400, "INVALID_REQUEST", "Idempotency-Key must be 1-128 printable ASCII characters without spaces"
        )
    return key


def _check_replay(store: MockStore, key: str | None, fp: str) -> IdempotencyRecord | None:
    """Return the stored record for *key* when it matches *fp*; raises 409 on mismatch."""
    if key is None:
        return None
    record = store.idempotency.get(key)
    if record is None:
        return None
    if record.expired(store.now()):
        del store.idempotency[key]
        return None
    if record.fingerprint != fp:
        raise ApiError(409, "CONFLICT", "Idempotency-Key was already used with a different request body")
    return record


def _remember(
    store: MockStore, key: str | None, fp: str, status_code: int, body: Any, *, operation_id: str | None = None
) -> None:
    """Store a response for replay under *key*."""
    if key is None:
        return
    now = store.now()
    store.idempotency[key] = IdempotencyRecord(
        key=key,
        fingerprint=fp,
        status_code=status_code,
        body=body,
        headers={},
        operation_id=operation_id,
        created_at=now,
        expires_at=now + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS if operation_id else 86400),
    )


def _replay_response(record: IdempotencyRecord) -> JSONResponse:
    """Rebuild a stored response with the ``Idempotency-Replayed`` marker."""
    return _json_response(record.status_code, record.body, {"Idempotency-Replayed": "true"})


def _replayable(store: MockStore, key: str | None, fp: str) -> JSONResponse | None:
    """Return a replay response when *key* was already committed with the same body."""
    record = _check_replay(store, key, fp)
    if record is not None:
        LOGGER.info("replaying idempotent response for key %s", key)
        return _replay_response(record)
    return None


# --------------------------------------------------------------------------- #
# domain helpers
# --------------------------------------------------------------------------- #
def _need_user(store: MockStore, user_id: str) -> dict[str, Any]:
    """Return a user or raise 404 ``USER_NOT_FOUND``."""
    user = store.users.get(user_id)
    if user is None:
        raise ApiError(404, "USER_NOT_FOUND", f"user {user_id} not found")
    _activate_next_plan(store, user)
    return user


def _activate_next_plan(store: MockStore, user: dict[str, Any]) -> None:
    queued = store.next_plans.get(user["id"])
    if not queued or queued["state"] != "queued" or user.get("deleted"):
        return
    if not user.get("enabled", True) and user.get("disable_reason") in ("manual", "admin_action"):
        return
    now = store.now()
    used = int(user.get("traffic_used_rx", 0)) + int(user.get("traffic_used_tx", 0))
    limit = user.get("traffic_limit_bytes")
    time_due = bool(user.get("expires_at") and parse_iso(user["expires_at"]) <= now)
    quota_due = limit is not None and used >= limit
    if not time_due and not quota_due:
        return
    fields = (
        "traffic_limit_bytes",
        "duration_seconds",
        "device_limit",
        "speed_limit_down_kbps",
        "speed_limit_up_kbps",
        "interface_id",
    )
    before = {k: user.get(k) for k in fields}
    before.update(
        expires_at=user.get("expires_at"),
        traffic_used_rx=user.get("traffic_used_rx", 0),
        traffic_used_tx=user.get("traffic_used_tx", 0),
    )
    terms = dict(queued["terms"])
    if len(store.devices_of(user["id"])) > (terms["device_limit"] or 100000):
        queued.update(state="needs_review", review_reason="device_limit")
        return
    if (
        time_due
        and not quota_due
        and queued["carry_unused_traffic"]
        and limit is not None
        and terms["traffic_limit_bytes"] is not None
    ):
        terms["traffic_limit_bytes"] += max(limit - used, 0)
    user.update({k: terms[k] for k in fields})
    user.update(
        template_id=queued["template_id"],
        traffic_used_rx=0,
        traffic_used_tx=0,
        traffic_used_total=0,
        activated_at=iso(now),
        expires_at=iso(now + timedelta(seconds=terms["duration_seconds"])) if terms["duration_seconds"] else None,
        enabled=True,
        disable_reason=None,
        updated_at=iso(now),
    )
    after = {k: user.get(k) for k in before}
    store.activations.setdefault(user["id"], []).append(
        {
            "activation_id": new_id("activation"),
            "user_id": user["id"],
            "template_id": queued["template_id"],
            "trigger": "quota" if quota_due else "time",
            "before": before,
            "after": after,
            "activated_at": iso(now),
        }
    )
    store.next_plans.pop(user["id"])


def _need_device(store: MockStore, device_id: str) -> dict[str, Any]:
    """Return a device or raise 404 ``DEVICE_NOT_FOUND``."""
    device = store.devices.get(device_id)
    if device is None:
        raise ApiError(404, "DEVICE_NOT_FOUND", f"device {device_id} not found")
    return device


def _need_plan(store: MockStore, template_id: str) -> dict[str, Any]:
    """Return a plan or raise 404 ``TEMPLATE_NOT_FOUND``."""
    plan = store.plans.get(template_id)
    if plan is None:
        raise ApiError(404, "TEMPLATE_NOT_FOUND", f"plan {template_id} not found")
    return plan


def _need_interface(store: MockStore, interface_id: str) -> dict[str, Any]:
    """Return an interface or raise 404 ``INTERFACE_NOT_FOUND``."""
    interface = store.interfaces.get(interface_id)
    if interface is None:
        raise ApiError(404, "INTERFACE_NOT_FOUND", f"interface {interface_id} not found")
    return interface


def _need_webhook(store: MockStore, webhook_id: str) -> dict[str, Any]:
    """Return a webhook endpoint or raise 404 ``WEBHOOK_NOT_FOUND``."""
    webhook = store.webhooks.get(webhook_id)
    if webhook is None:
        raise ApiError(404, "WEBHOOK_NOT_FOUND", f"webhook {webhook_id} not found")
    return webhook


def _new_user(store: MockStore, fields: dict[str, Any], now: datetime) -> dict[str, Any]:
    """Create a user document in the store (subscription token included)."""
    duration = fields.get("duration_seconds")
    start_policy = fields.get("start_policy") or "immediate"
    activated = now if start_policy == "immediate" else None
    expires = activated + timedelta(seconds=int(duration)) if activated is not None and duration is not None else None
    stamp = iso(now)
    user: dict[str, Any] = {
        "id": new_id("usr"),
        "username": fields["username"],
        "display_name": fields.get("display_name") or fields["username"],
        "note": fields.get("note"),
        "tags": list(fields.get("tags") or []),
        "status": "active" if activated else "waiting_first_connection",
        "disable_reason": None,
        "traffic_limit_bytes": fields.get("traffic_limit_bytes"),
        "traffic_used_rx": int(fields.get("traffic_used_rx") or 0),
        "traffic_used_tx": int(fields.get("traffic_used_tx") or 0),
        "traffic_used_total": 0,
        "speed_limit_down_kbps": fields.get("speed_limit_down_kbps"),
        "speed_limit_up_kbps": fields.get("speed_limit_up_kbps"),
        "device_limit": fields.get("device_limit"),
        "template_id": fields.get("template_id"),
        "interface_id": fields.get("interface_id"),
        "start_policy": start_policy,
        "duration_seconds": duration,
        "activated_at": iso(activated),
        "expires_at": iso(expires),
        "last_activity_at": None,
        "enabled": bool(fields.get("enabled", True)),
        "metadata": dict(fields.get("metadata") or {}),
        "deleted": False,
        "created_at": stamp,
        "updated_at": stamp,
        "sub_token": secrets.token_urlsafe(32),
    }
    store.users[user["id"]] = user
    return user


def _new_device(
    store: MockStore, user: dict[str, Any], name: str, interface_id: str | None, preshared_key: bool, now: datetime
) -> dict[str, Any]:
    """Create a device with a server-side keypair and the next free address."""
    iface_id = interface_id or user.get("interface_id")
    if iface_id not in store.interfaces:
        iface_id = next(iter(store.interfaces), None)
    if iface_id is None:
        raise ApiError(409, "NODE_UNAVAILABLE", "no tunnel interface is configured on this node")
    address = store.allocate_ipv4(iface_id)
    if address is None:
        raise ApiError(409, "CONFLICT", "address pool exhausted")
    stamp = iso(now)
    device: dict[str, Any] = {
        "id": new_id("dev"),
        "user_id": user["id"],
        "interface_id": iface_id,
        "name": name,
        "ipv4_address": address,
        "public_key": new_wg_key(),
        "enabled": True,
        "last_handshake_at": None,
        "last_endpoint": "",
        "rx_bytes": 0,
        "tx_bytes": 0,
        "created_at": stamp,
        "updated_at": stamp,
    }
    store.devices[device["id"]] = device
    store.device_secrets[device["id"]] = {
        "private_key": new_wg_key(),
        "preshared_key": new_wg_key() if preshared_key else "",
    }
    return device


def _provision_purchase(
    store: MockStore, plan: dict[str, Any], username: str, device_name: str, device_count: int = 1
) -> dict[str, Any]:
    """Atomically commit user + first device + subscription link for a purchase."""
    now = store.now()
    user = _new_user(
        store,
        {
            "username": username,
            "display_name": username,
            "template_id": plan["id"],
            "interface_id": plan.get("interface_id"),
            "traffic_limit_bytes": plan.get("traffic_limit_bytes"),
            "duration_seconds": plan.get("duration_seconds"),
            "device_limit": plan.get("device_limit"),
            "speed_limit_down_kbps": plan.get("speed_limit_down_kbps"),
            "speed_limit_up_kbps": plan.get("speed_limit_up_kbps"),
            "start_policy": plan.get("start_policy", "immediate"),
        },
        now,
    )
    names = [device_name] if device_count == 1 else [f"{device_name}-{i}" for i in range(1, device_count + 1)]
    try:
        if any(not name.strip() or len(name.encode("utf-8")) > 64 for name in names):
            raise ApiError(400, "INVALID_REQUEST", "invalid device name")
        devices = [_new_device(store, user, name, plan.get("interface_id"), False, now) for name in names]
    except BaseException:
        for created in store.devices_of(user["id"]):
            store.devices.pop(created["id"], None)
            store.device_secrets.pop(created["id"], None)
        store.users.pop(user["id"], None)
        raise
    return {
        "operation_id": new_id("op"),
        "kind": "purchase",
        "state": "committed",
        "user_id": user["id"],
        "device_id": devices[0]["id"],
        "device_ids": [item["id"] for item in devices],
        "template_id": plan["id"],
        "created_at": store.now_iso(),
    }


def _counts(store: MockStore) -> dict[str, int]:
    """Node-wide counters shared by ``/node/stats`` and ``/stats``."""
    users = [serialize_user(user, store.now()) for user in store.users.values()]
    devices = list(store.devices.values())
    return {
        "users_total": len(users),
        "users_active": sum(1 for u in users if u["status"] == "active"),
        "users_enabled": sum(1 for u in users if u["enabled"]),
        "users_expired": sum(1 for u in users if u["status"] == "expired"),
        "users_traffic_exceeded": sum(1 for u in users if u["status"] == "traffic_exceeded"),
        "users_deleted": sum(1 for u in users if u["deleted"]),
        "users_online": sum(1 for u in users if _is_online(u.get("last_activity_at"), store.now())),
        "devices_total": len(devices),
        "devices_enabled": sum(1 for d in devices if d["enabled"]),
        "interfaces_total": len(store.interfaces),
        "interfaces_enabled": sum(1 for i in store.interfaces.values() if i["enabled"]),
        "plans_total": len(store.plans),
        "webhooks_total": len(store.webhooks),
        "traffic_rx_bytes": sum(int(d.get("rx_bytes", 0)) for d in devices),
        "traffic_tx_bytes": sum(int(d.get("tx_bytes", 0)) for d in devices),
    }


def _is_online(last_handshake: str | None, now: datetime) -> bool:
    """A peer counts as online when it handshook within the last 180 seconds."""
    if not last_handshake:
        return False
    try:
        return (now - parse_iso(last_handshake)).total_seconds() <= 180
    except ValueError:  # pragma: no cover - defensive
        return False


def _node_document(store: MockStore) -> dict[str, Any]:
    """Build the ``Node`` document."""
    return {
        "version": store.config.version,
        "node_id": store.config.node_id,
        "tools_version": store.config.version,
        "uptime_seconds": store.uptime_seconds(),
        "interfaces": [
            {
                "id": iface["id"],
                "name": iface["name"],
                "listen_port": iface["listen_port"],
                "ipv4_subnet": iface["ipv4_subnet"],
                "enabled": iface["enabled"],
                "backend_mode": iface["backend_mode"],
                "devices": sum(1 for d in store.devices.values() if d["interface_id"] == iface["id"]),
            }
            for iface in store.interfaces.values()
        ],
    }


def _telemetry_points(store: MockStore, count: int) -> list[dict[str, Any]]:
    """Synthesize a plausible chronological ``TelemetryPoint`` history."""
    now = store.now()
    devices = list(store.devices.values())
    vpn_rx = sum(int(d.get("rx_bytes", 0)) for d in devices)
    vpn_tx = sum(int(d.get("tx_bytes", 0)) for d in devices)
    online = sum(1 for d in devices if _is_online(d.get("last_handshake_at"), now))
    enabled_ifaces = sum(1 for i in store.interfaces.values() if i["enabled"])
    observed_ifaces = len(store.interfaces)
    uptime = store.uptime_seconds()
    points: list[dict[str, Any]] = []
    for index in range(count):
        drift = index + 1
        points.append(
            {
                "timestamp": iso(now - timedelta(seconds=TELEMETRY_CADENCE_SECONDS * (count - 1 - index))),
                "health": "healthy",
                "issues": [],
                "cpu_percent": round(4.0 + (drift % 7) * 0.9, 2),
                "memory_used_bytes": 512 * 1024**2 + drift * 1024**2,
                "memory_total_bytes": 4 * 1024**3,
                "memory_percent": round(12.5 + (drift % 5) * 0.4, 2),
                "disk_used_bytes": 20 * 1024**3,
                "disk_total_bytes": 80 * 1024**3,
                "disk_percent": 25.0,
                "load_1": round(0.2 + (drift % 4) * 0.15, 2),
                "uptime_seconds": max(uptime, 1),
                "process_rss_bytes": 96 * 1024**2,
                "process_heap_bytes": 32 * 1024**2,
                "goroutines": 24 + drift % 5,
                "host_rx_bytes": 10 * 1024**3 + drift * 4096,
                "host_tx_bytes": 8 * 1024**3 + drift * 2048,
                "vpn_rx_bytes": vpn_rx + drift * 1024,
                "vpn_tx_bytes": vpn_tx + drift * 512,
                "host_rx_bytes_per_second": 120.5,
                "host_tx_bytes_per_second": 64.25,
                "vpn_rx_bytes_per_second": 42.0,
                "vpn_tx_bytes_per_second": 21.0,
                "online_users": online,
                "active_peers": online,
                "enabled_interfaces": enabled_ifaces,
                "observed_interfaces": observed_ifaces,
            }
        )
    return points


# --- client configuration --------------------------------------------------- #
def _config_text(
    store: MockStore,
    user: dict[str, Any],
    device: dict[str, Any],
    iface: dict[str, Any],
    private_key: str,
    preshared_key: str,
) -> str:
    """Render the canonical AmneziaWG client configuration (one trailing newline)."""
    obf = {**DEFAULT_OBFUSCATION, **(iface.get("obfuscation") or {})}
    dns = store.settings.get("node.dns") or subnet_dns(iface["ipv4_subnet"])
    lines = [
        "[Interface]",
        f"PrivateKey = {private_key}",
        f"Address = {device['ipv4_address']}/32",
        f"DNS = {dns}",
        f"MTU = {iface['mtu']}",
        f"Jc = {obf['jc']}",
        f"Jmin = {obf['jmin']}",
        f"Jmax = {obf['jmax']}",
        f"S1 = {obf['s1']}",
        f"S2 = {obf['s2']}",
        f"H1 = {obf['h1']}",
        f"H2 = {obf['h2']}",
        f"H3 = {obf['h3']}",
        f"H4 = {obf['h4']}",
        "",
        "[Peer]",
        f"PublicKey = {iface['public_key']}",
    ]
    if preshared_key:
        lines.append(f"PresharedKey = {preshared_key}")
    lines += [
        "AllowedIPs = 0.0.0.0/0, ::/0",
        f"Endpoint = {iface.get('endpoint_override') or store.settings.get('node.endpoint', 'vpn.mock.local')}",
        "PersistentKeepalive = 25",
        "",
    ]
    LOGGER.debug("rendered client config for user %s device %s", user["id"], device["id"])
    return "\n".join(lines)


def _config_stem(store: MockStore, user: dict[str, Any], device: dict[str, Any]) -> str:
    """Short ASCII label + stable eight-character device code."""
    prefix = str(store.settings.get("downloads.filename_prefix") or "")[:2]
    suffix = str(store.settings.get("downloads.filename_suffix") or "")[:2]
    label = "".join(ch for ch in user["username"] if ch.isascii() and ch.isalnum())[:6]
    code = hashlib.sha256(device["id"].encode("utf-8")).hexdigest()[:8]
    return f"{prefix}{label}{suffix}-{code}"[: 15 + 9]


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """One PNG chunk with its CRC."""
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _qr_matrix(payload: str, modules: int = 25) -> list[list[bool]]:
    """Build a QR-looking module matrix (finder patterns + digest-derived data)."""
    bits = "".join(f"{byte:08b}" for byte in hashlib.sha256(payload.encode("utf-8")).digest()) * 8
    grid = [[bits[(y * modules + x) % len(bits)] == "1" for x in range(modules)] for y in range(modules)]

    def finder(ox: int, oy: int) -> None:
        for dy in range(7):
            for dx in range(7):
                ring = dx in (0, 6) or dy in (0, 6)
                grid[oy + dy][ox + dx] = ring or (2 <= dx <= 4 and 2 <= dy <= 4)

    finder(0, 0)
    finder(modules - 7, 0)
    finder(0, modules - 7)
    for index in range(8):
        grid[7][index] = grid[index][7] = False
        grid[7][modules - 1 - index] = grid[index][modules - 8] = False
        grid[modules - 8][index] = grid[modules - 1 - index][7] = False
    return grid


def _png_bytes(matrix: list[list[bool]], scale: int = 4, border: int = 4) -> bytes:
    """Encode a module matrix as an 8-bit greyscale PNG (stdlib only)."""
    modules = len(matrix)
    size = (modules + 2 * border) * scale
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type 0 (None)
        row = y // scale - border
        for x in range(size):
            col = x // scale - border
            dark = 0 <= row < modules and 0 <= col < modules and matrix[row][col]
            raw.append(0 if dark else 255)
    header = struct.pack(">IIBBBBB", size, size, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


# --- settings / webhooks ---------------------------------------------------- #
def _settings_catalog(store: MockStore) -> dict[str, Any]:
    """Build the settings catalog with secrets redacted to ``secret_set``."""
    items: list[dict[str, Any]] = []
    for key in sorted(store.settings):
        value = store.settings[key]
        secret = any(word in key.lower() for word in ("secret", "password", "token", "private_key"))
        items.append(
            {
                "key": key,
                "value": None if secret else value,
                "type": type(value).__name__,
                "secret_set": bool(secret and value),
                "description": "mock setting",
            }
        )
    return {"items": items, "settings": {item["key"]: item["value"] for item in items}}


def _validate_events(events: list[str]) -> list[str]:
    """Validate webhook event names against the documented enum."""
    unknown = [event for event in events if event not in WEBHOOK_EVENTS]
    if unknown:
        raise ApiError(400, "INVALID_REQUEST", f"unknown webhook event(s): {', '.join(unknown)}")
    return list(events)


def _serialize_webhook(store: MockStore, webhook: dict[str, Any]) -> dict[str, Any]:
    """Attach delivery statistics and hide the signing secret."""
    receipts = store.deliveries.get(webhook["id"], [])
    return {
        "id": webhook["id"],
        "include_reseller_events": bool(webhook.get("include_reseller_events", False)),
        "url": webhook["url"],
        "enabled": webhook["enabled"],
        "events": list(webhook["events"]),
        "created_at": webhook["created_at"],
        "stats": {
            "pending": sum(1 for r in receipts if r["status"] == "pending"),
            "delivered": sum(1 for r in receipts if r["status"] == "delivered"),
            "dead": sum(1 for r in receipts if r["status"] == "dead"),
        },
    }


def _new_receipt(store: MockStore, event_type: str, status: str = "delivered", attempts: int = 1) -> dict[str, Any]:
    """Create one non-secret delivery receipt."""
    stamp = store.now_iso()
    return {
        "id": new_id("dlv"),
        "event_id": new_id("evt"),
        "event_type": event_type,
        "status": status,
        "attempts": attempts,
        "next_attempt_at": None,
        "created_at": stamp,
        "updated_at": stamp,
    }


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
router = APIRouter()


# --- ops (public) ----------------------------------------------------------- #
@router.get("/healthz", tags=["ops"])
async def healthz() -> dict[str, str]:
    """Public liveness probe."""
    return {"status": "ok"}


@router.get("/readyz", tags=["ops"])
async def readyz(request: Request) -> Response:
    """Public readiness probe (503 while the mock is marked unready)."""
    ready = _store(request).config.ready
    return _json_response(200 if ready else 503, {"status": "ready" if ready else "not_ready"})


@router.get("/api/v1/node/health", tags=["node"])
async def node_health(request: Request) -> dict[str, str]:
    """Public capability discovery: version + liveness only."""
    return {"status": "ok", "version": _store(request).config.version}


# --- node ------------------------------------------------------------------- #
@router.get("/api/v1/node", tags=["node"], dependencies=[Depends(require("node.read"))])
async def get_node(request: Request) -> dict[str, Any]:
    """Node identity, uptime and interface inventory."""
    store = _store(request)
    async with store.lock:
        return _node_document(store)


@router.get("/api/v1/node/stats", tags=["node"], dependencies=[Depends(require("node.read"))])
async def get_node_stats(request: Request) -> dict[str, Any]:
    """Runtime counters (uptime, user/device counts)."""
    store = _store(request)
    async with store.lock:
        return {"users": len(store.users), "devices": len(store.devices), "uptime_seconds": store.uptime_seconds()}


@router.get("/api/v1/node/telemetry", tags=["stats"], dependencies=[Depends(require("stats.read"))])
async def get_node_telemetry(request: Request, points: str | None = None) -> dict[str, Any]:
    """Bounded live node and VPN telemetry (newest samples, chronological)."""
    store = _store(request)
    count = 60
    if points is not None:
        try:
            count = int(points)
        except ValueError:
            raise ApiError(400, "INVALID_REQUEST", "points must be an integer between 1 and 180") from None
    count = max(1, min(TELEMETRY_MAX_POINTS, count))
    async with store.lock:
        history = _telemetry_points(store, count)
        return {
            "cadence_seconds": TELEMETRY_CADENCE_SECONDS,
            "latest": history[-1] if history else None,
            "points": history,
        }


# --- purchases / operations ------------------------------------------------- #
@router.post("/api/v1/purchases", tags=["integration"], dependencies=[Depends(require("purchases.create"))])
async def create_purchase(request: Request, payload: PurchaseRequest) -> Response:
    """Provision a customer, first device and subscription link atomically."""
    store = _store(request)
    key = _idempotency_key(request, required=True)
    body = payload.model_dump()
    body["device_count"] = payload.device_count or 1
    body["device_name"] = body["device_name"].strip()
    fp = fingerprint({"route": "purchases", **body})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        plan = _need_plan(store, body["template_id"])
        if not plan.get("enabled", True):
            raise ApiError(400, "INVALID_REQUEST", f"plan {plan['id']} is disabled")
        username = body.get("username") or derived_username(key or "")
        if any(user["username"] == username for user in store.users.values()):
            raise ApiError(409, "USERNAME_EXISTS", f"username {username} already exists")
        if plan.get("device_limit") is not None and body["device_count"] > plan["device_limit"]:
            raise ApiError(409, "DEVICE_LIMIT_REACHED", "plan does not allow any device")
        result = _provision_purchase(store, plan, username, body.get("device_name") or "device-1", body["device_count"])
        store.operations[key or ""] = {
            "key": key,
            "result": result,
            "created_at": store.now_iso(),
            "expires_at": iso(store.now() + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)),
        }
        _remember(store, key, fp, 201, result, operation_id=result["operation_id"])
        return _json_response(201, result)


@router.get("/api/v1/operations/result", tags=["integration"], dependencies=[Depends(require("operations.read"))])
async def get_operation_result(request: Request) -> dict[str, Any]:
    """Recover a committed integration result by ``Idempotency-Key``."""
    store = _store(request)
    key = _idempotency_key(request, required=True)
    async with store.lock:
        record = store.operations.get(key or "")
        if record is None or parse_iso(record["expires_at"]) <= store.now():
            raise ApiError(404, "OPERATION_NOT_FOUND", "no committed operation result for this key")
        return dict(record["result"])


# --- users ------------------------------------------------------------------ #
@router.post("/api/v1/users/{user_id}/quota/add", dependencies=[Depends(require("users.update"))])
async def top_up_quota(request: Request, user_id: str, payload: QuotaAdd) -> Response:
    store = _store(request)
    key = _idempotency_key(request, required=True)
    fp = fingerprint({"route": "quota.add", "user_id": user_id, "bytes": payload.bytes})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        user = _need_user(store, user_id)
        if user.get("traffic_limit_bytes") is None:
            raise ApiError(409, "CONFLICT", "unlimited quota cannot be topped up")
        fields = (
            "traffic_limit_bytes",
            "traffic_used_rx",
            "traffic_used_tx",
            "traffic_used_total",
            "enabled",
            "disable_reason",
        )
        before = {k: serialize_user(user, store.now()).get(k) for k in fields}
        user["traffic_limit_bytes"] += payload.bytes
        if user.get("disable_reason") == "traffic_limit" and user["traffic_limit_bytes"] > before["traffic_used_total"]:
            user.update(enabled=True, disable_reason=None)
        after = {k: serialize_user(user, store.now()).get(k) for k in fields}
        result = {
            "operation_id": new_id("op"),
            "kind": "quota_top_up",
            "state": "committed",
            "user_id": user_id,
            "before": before,
            "after": after,
            "created_at": store.now_iso(),
        }
        store.operations[key] = {
            "result": result,
            "expires_at": iso(store.now() + timedelta(seconds=IDEMPOTENCY_TTL_SECONDS)),
        }
        _remember(store, key, fp, 200, result, operation_id=result["operation_id"])
        return _json_response(200, result)


@router.get("/api/v1/users/{user_id}/next-plan", dependencies=[Depends(require("next_plans.read"))])
async def get_next_plan(request: Request, user_id: str) -> dict[str, Any]:
    store = _store(request)
    async with store.lock:
        _need_user(store, user_id)
        return {"next_plan": store.next_plans.get(user_id)}


@router.put("/api/v1/users/{user_id}/next-plan", dependencies=[Depends(require("next_plans.write"))])
async def put_next_plan(request: Request, user_id: str, payload: NextPlanRequest) -> Response:
    store = _store(request)
    key = _idempotency_key(request, required=False)
    fp = fingerprint({"route": "next-plan.put", "user_id": user_id, **payload.model_dump()})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        _need_user(store, user_id)
        template = _need_plan(store, payload.template_id)
        if not template["enabled"]:
            raise ApiError(400, "INVALID_REQUEST", "template is disabled")
        terms = {
            k: template.get(k)
            for k in (
                "name",
                "traffic_limit_bytes",
                "duration_seconds",
                "device_limit",
                "speed_limit_down_kbps",
                "speed_limit_up_kbps",
                "interface_id",
            )
        }
        result = {
            "user_id": user_id,
            "template_id": payload.template_id,
            "terms": terms,
            "carry_unused_traffic": payload.carry_unused_traffic,
            "state": "queued",
            "created_at": store.now_iso(),
        }
        store.next_plans[user_id] = result
        _remember(store, key, fp, 200, result)
        return _json_response(200, result)


@router.delete("/api/v1/users/{user_id}/next-plan", dependencies=[Depends(require("next_plans.write"))])
async def delete_next_plan(request: Request, user_id: str) -> Response:
    store = _store(request)
    async with store.lock:
        _need_user(store, user_id)
        store.next_plans.pop(user_id, None)
        return Response(status_code=204)


@router.get("/api/v1/users/{user_id}/next-plan/activations", dependencies=[Depends(require("next_plans.read"))])
async def next_plan_activations(request: Request, user_id: str, limit: int = 20) -> dict[str, Any]:
    store = _store(request)
    async with store.lock:
        _need_user(store, user_id)
        return {"items": list(reversed(store.activations.get(user_id, [])))[: max(1, min(limit, 100))]}


@router.post("/api/v1/users", tags=["users"], dependencies=[Depends(require("users.create"))])
async def create_user(request: Request, payload: UserCreate) -> Response:
    """Create a user (idempotent when ``Idempotency-Key`` is supplied)."""
    store = _store(request)
    key = _idempotency_key(request, required=False)
    body = payload.model_dump()
    fp = fingerprint({"route": "users.create", **body})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        if any(user["username"] == body["username"] for user in store.users.values()):
            raise ApiError(409, "USERNAME_EXISTS", f"username {body['username']} already exists")
        if body.get("template_id"):
            template = _need_plan(store, body["template_id"])
            if not template["enabled"]:
                raise ApiError(400, "INVALID_REQUEST", "template is disabled")
            for key in (
                "traffic_limit_bytes",
                "duration_seconds",
                "start_policy",
                "device_limit",
                "speed_limit_down_kbps",
                "speed_limit_up_kbps",
                "interface_id",
            ):
                body[key] = template.get(key)

        if body.get("interface_id"):
            _need_interface(store, body["interface_id"])
        result = serialize_user(_new_user(store, body, store.now()), store.now())
        _remember(store, key, fp, 201, result)
        return _json_response(201, result)


@router.get("/api/v1/users", tags=["users"], dependencies=[Depends(require("users.read"))])
async def list_users(
    request: Request,
    limit: int = 50,
    cursor: str | None = None,
    sort: str = "created_at",
    order: str | None = None,
    status: str | None = None,
    username: str | None = None,
    enabled: bool | None = None,
    template_id: str | None = None,
    interface_id: str | None = None,
    traffic_exceeded: bool | None = None,
    expires_before: str | None = None,
    expires_after: str | None = None,
    created_before: str | None = None,
    created_after: str | None = None,
) -> dict[str, Any]:
    """List users with cursor pagination and the documented filters."""
    store = _store(request)
    if sort not in PLAN_SORTS:
        raise ApiError(400, "INVALID_REQUEST", f"unsupported sort '{sort}'")
    if order is not None and order not in ("asc", "desc"):
        raise ApiError(400, "INVALID_REQUEST", f"unsupported order '{order}'")
    offset = 0
    if cursor:
        try:
            offset = decode_cursor(cursor)
        except ValueError:
            raise ApiError(400, "INVALID_REQUEST", "malformed cursor") from None

    def bound(value: str, name: str) -> datetime:
        try:
            return parse_iso(value)
        except ValueError:
            raise ApiError(400, "INVALID_REQUEST", f"{name} must be an RFC 3339 date-time") from None

    async with store.lock:
        items = [serialize_user(user, store.now()) for user in store.users.values()]
        if username:
            items = [u for u in items if username in (u["username"] or "")]
        if status:
            items = [u for u in items if u["status"] == status]
        if enabled is not None:
            items = [u for u in items if bool(u["enabled"]) is enabled]
        if template_id:
            items = [u for u in items if u["template_id"] == template_id]
        if interface_id:
            items = [u for u in items if u["interface_id"] == interface_id]
        if traffic_exceeded is not None:
            items = [u for u in items if (u["status"] == "traffic_exceeded") is traffic_exceeded]
        if expires_before:
            edge = bound(expires_before, "expires_before")
            items = [u for u in items if u["expires_at"] and parse_iso(u["expires_at"]) <= edge]
        if expires_after:
            edge = bound(expires_after, "expires_after")
            items = [u for u in items if u["expires_at"] and parse_iso(u["expires_at"]) >= edge]
        if created_before:
            edge = bound(created_before, "created_before")
            items = [u for u in items if parse_iso(u["created_at"]) <= edge]
        if created_after:
            edge = bound(created_after, "created_after")
            items = [u for u in items if parse_iso(u["created_at"]) >= edge]
        keys: dict[str, Callable[[dict[str, Any]], Any]] = {
            "created_at": lambda u: u["created_at"] or "",
            "username": lambda u: u["username"] or "",
            "expires_at": lambda u: u["expires_at"] or "",
            "used": lambda u: u["traffic_used_total"] or 0,
        }
        reverse = (order or ("desc" if sort == "created_at" else "asc")) == "desc"
        items.sort(key=keys[sort], reverse=reverse)
        page = items[offset : offset + max(1, min(500, limit))]
        next_offset = offset + len(page)
        return {"items": page, "next_cursor": encode_cursor(next_offset) if next_offset < len(items) else ""}


@router.get("/api/v1/users/{user_id}", tags=["users"], dependencies=[Depends(require("users.read"))])
async def get_user(request: Request, user_id: str) -> dict[str, Any]:
    """Get one user."""
    store = _store(request)
    async with store.lock:
        return serialize_user(_need_user(store, user_id), store.now())


@router.patch("/api/v1/users/{user_id}", tags=["users"], dependencies=[Depends(require("users.update"))])
async def patch_user(request: Request, user_id: str, payload: UserPatch) -> dict[str, Any]:
    """Partially update a user (username immutable; ``null`` clears a limit)."""
    store = _store(request)
    patch = payload.model_dump(exclude_unset=True)
    if set(patch) - set(USER_PATCHABLE):
        raise ApiError(400, "INVALID_REQUEST", "username is immutable")
    async with store.lock:
        user = _need_user(store, user_id)
        if patch.get("template_id"):
            _need_plan(store, patch["template_id"])
        if patch.get("interface_id"):
            _need_interface(store, patch["interface_id"])
        patch.pop("metadata", None)
        for key in ("display_name", "note", "tags", "duration_seconds", "enabled"):
            if patch.get(key) is None:
                patch.pop(key, None)
        user.update({key: value for key, value in patch.items() if key in USER_PATCHABLE})
        user["updated_at"] = store.now_iso()
        return serialize_user(user, store.now())


@router.delete("/api/v1/users/{user_id}", tags=["users"], dependencies=[Depends(require("users.delete"))])
async def delete_user(request: Request, user_id: str) -> dict[str, Any]:
    """Soft-delete a user (username stays reserved; peers removed)."""
    store = _store(request)
    async with store.lock:
        user = _need_user(store, user_id)
        for device in store.devices_of(user_id):
            store.devices.pop(device["id"], None)
            store.device_secrets.pop(device["id"], None)
        user.update(deleted=True, enabled=False, updated_at=store.now_iso())
        return {"id": user["id"], "deleted": True}


# --- subscription ----------------------------------------------------------- #
@router.get(
    "/api/v1/users/{user_id}/subscription", tags=["integration"], dependencies=[Depends(require("subscriptions.read"))]
)
async def get_subscription(request: Request, user_id: str) -> dict[str, str]:
    """Retrieve a customer's private subscription link."""
    store = _store(request)
    async with store.lock:
        user = _need_user(store, user_id)
        return {"path": f"/sub/{user['sub_token']}"}


@router.post(
    "/api/v1/users/{user_id}/subscription/rotate",
    tags=["integration"],
    dependencies=[Depends(require("subscriptions.rotate"))],
)
async def rotate_subscription(request: Request, user_id: str) -> dict[str, Any]:
    """Revoke the old link and every old device configuration."""
    store = _store(request)
    async with store.lock:
        user = _need_user(store, user_id)
        user["sub_token"] = secrets.token_urlsafe(32)
        rotated = 0
        for device in store.devices_of(user_id):
            secrets_map = store.device_secrets.setdefault(device["id"], {})
            secrets_map["private_key"] = new_wg_key()
            if secrets_map.get("preshared_key"):
                secrets_map["preshared_key"] = new_wg_key()
            device.update(public_key=new_wg_key(), updated_at=store.now_iso())
            rotated += 1
        return {"path": f"/sub/{user['sub_token']}", "devices_rotated": rotated}


# --- user lifecycle --------------------------------------------------------- #
@router.post("/api/v1/users/{user_id}/enable", tags=["users"], dependencies=[Depends(require("users.update"))])
async def enable_user(request: Request, user_id: str) -> dict[str, Any]:
    """Enable a user (returns to active/waiting)."""
    store = _store(request)
    async with store.lock:
        user = _need_user(store, user_id)
        user.update(enabled=True, disable_reason=None, updated_at=store.now_iso())
        return serialize_user(user, store.now())


@router.post("/api/v1/users/{user_id}/disable", tags=["users"], dependencies=[Depends(require("users.update"))])
async def disable_user(
    request: Request, user_id: str, payload: DisableRequest | None = Body(default=None)
) -> dict[str, Any]:
    """Disable a user."""
    store = _store(request)
    reason = (payload or DisableRequest()).reason
    async with store.lock:
        user = _need_user(store, user_id)
        user.update(enabled=False, disable_reason=reason, updated_at=store.now_iso())
        return serialize_user(user, store.now())


@router.post("/api/v1/users/{user_id}/renew", tags=["users"], dependencies=[Depends(require("users.update"))])
async def renew_user(request: Request, user_id: str, payload: RenewRequest) -> Response:
    """Renew a subscription (idempotent with ``Idempotency-Key``)."""
    store = _store(request)
    key = _idempotency_key(request, required=False)
    body = payload.model_dump()
    fp = fingerprint({"route": "users.renew", "user_id": user_id, **body})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        user = _need_user(store, user_id)
        now = store.now()
        duration = int(body.get("duration_seconds") or user.get("duration_seconds") or 30 * 24 * 3600)
        if body["mode"] == "exact":
            if not body.get("exact"):
                raise ApiError(400, "INVALID_REQUEST", "'exact' is required when mode=exact")
            expiry = parse_iso(body["exact"])
        elif body["mode"] == "from_now":
            expiry = now + timedelta(seconds=duration)
        else:
            current = parse_iso(user["expires_at"]) if user.get("expires_at") else now
            expiry = max(current, now) + timedelta(seconds=duration)
        user.update(
            expires_at=iso(expiry),
            duration_seconds=duration,
            enabled=True,
            disable_reason=None,
            updated_at=store.now_iso(),
        )
        refresh_user(user, now)  # traffic_exceeded is NOT reactivated here
        result = serialize_user(user, store.now())
        _remember(store, key, fp, 200, result)
        return _json_response(200, result)


# --- traffic ---------------------------------------------------------------- #
@router.get("/api/v1/users/{user_id}/traffic", tags=["stats"], dependencies=[Depends(require("traffic.read"))])
async def get_user_traffic(
    request: Request, user_id: str, granularity: str = "samples", hours: int = 48
) -> dict[str, Any]:
    """Traffic time series for the user's devices."""
    store = _store(request)
    if granularity not in ("samples", "hourly", "daily"):
        raise ApiError(400, "INVALID_REQUEST", f"unsupported granularity '{granularity}'")
    async with store.lock:
        user = _need_user(store, user_id)
        # the charged counters are the aggregate of the user's devices
        rx, tx = int(user.get("traffic_used_rx", 0)), int(user.get("traffic_used_tx", 0))
        cap = {"samples": 48, "hourly": 720, "daily": 8760}[granularity]
        count = max(1, min(int(hours) if hours else 1, cap))
        step = {"samples": timedelta(minutes=5), "hourly": timedelta(hours=1), "daily": timedelta(days=1)}[granularity]
        now = store.now()
        series = []
        for index in range(count):
            share_rx, share_tx = rx // count, tx // count
            if index == count - 1:
                share_rx, share_tx = rx - share_rx * (count - 1), tx - share_tx * (count - 1)
            series.append({"ts": iso(now - step * (count - 1 - index)), "rx": share_rx, "tx": share_tx})
        return {"granularity": granularity, "series": series}


def _apply_traffic(store: MockStore, user: dict[str, Any], rx: int | None, tx: int | None) -> dict[str, Any]:
    """Apply an absolute/additive traffic mutation and refresh lifecycle flags."""
    if rx is not None:
        user["traffic_used_rx"] = max(0, rx)
    if tx is not None:
        user["traffic_used_tx"] = max(0, tx)
    user["updated_at"] = store.now_iso()
    return serialize_user(user, store.now())


@router.post("/api/v1/users/{user_id}/traffic/add", tags=["users"], dependencies=[Depends(require("traffic.update"))])
async def add_user_traffic(request: Request, user_id: str, payload: TrafficAdd) -> Response:
    """Add traffic to the charged counters (idempotent with ``Idempotency-Key``)."""
    store = _store(request)
    key = _idempotency_key(request, required=False)
    body = payload.model_dump()
    fp = fingerprint({"route": "traffic.add", "user_id": user_id, **body})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        user = _need_user(store, user_id)
        result = _apply_traffic(
            store,
            user,
            int(user.get("traffic_used_rx", 0)) + body["rx_bytes"],
            int(user.get("traffic_used_tx", 0)) + body["tx_bytes"],
        )
        _remember(store, key, fp, 200, result)
        return _json_response(200, result)


@router.post("/api/v1/users/{user_id}/traffic/set", tags=["users"], dependencies=[Depends(require("traffic.update"))])
async def set_user_traffic(request: Request, user_id: str, payload: TrafficSet) -> Response:
    """Set the charged counters to absolute values (``null`` leaves one untouched)."""
    store = _store(request)
    key = _idempotency_key(request, required=False)
    body = payload.model_dump()
    fp = fingerprint({"route": "traffic.set", "user_id": user_id, **body})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        result = _apply_traffic(store, _need_user(store, user_id), body["rx_bytes"], body["tx_bytes"])
        _remember(store, key, fp, 200, result)
        return _json_response(200, result)


@router.post("/api/v1/users/{user_id}/traffic/reset", tags=["users"], dependencies=[Depends(require("traffic.update"))])
async def reset_user_traffic(request: Request, user_id: str) -> Response:
    """Reset usage to zero (one-op unblock)."""
    store = _store(request)
    key = _idempotency_key(request, required=False)
    fp = fingerprint({"route": "traffic.reset", "user_id": user_id})
    async with store.lock:
        replay = _replayable(store, key, fp)
        if replay is not None:
            return replay
        user = _need_user(store, user_id)
        for device in store.devices_of(user_id):
            device.update(rx_bytes=0, tx_bytes=0, updated_at=store.now_iso())
        result = _apply_traffic(store, user, 0, 0)
        _remember(store, key, fp, 200, result)
        return _json_response(200, result)


# --- devices ---------------------------------------------------------------- #
@router.get("/api/v1/users/{user_id}/devices", tags=["devices"], dependencies=[Depends(require("devices.read"))])
async def list_devices(request: Request, user_id: str) -> dict[str, Any]:
    """List the user's devices."""
    store = _store(request)
    async with store.lock:
        _need_user(store, user_id)
        return {"items": [serialize_device(device) for device in store.devices_of(user_id)]}


@router.post(
    "/api/v1/users/{user_id}/devices",
    tags=["devices"],
    status_code=201,
    dependencies=[Depends(require("devices.write"))],
)
async def create_device(request: Request, user_id: str, payload: DeviceCreate) -> dict[str, Any]:
    """Create a device (keys generated server-side)."""
    store = _store(request)
    async with store.lock:
        user = _need_user(store, user_id)
        devices = store.devices_of(user_id)
        limit = user.get("device_limit")
        if limit is not None and len(devices) >= int(limit):
            raise ApiError(409, "DEVICE_LIMIT_REACHED", f"user {user_id} reached its device limit of {limit}")
        if any(device["name"] == payload.name for device in devices):
            raise ApiError(409, "CONFLICT", f"device name {payload.name} already exists for this user")
        if payload.interface_id:
            _need_interface(store, payload.interface_id)
        return serialize_device(
            _new_device(store, user, payload.name, payload.interface_id, payload.preshared_key, store.now())
        )


@router.get("/api/v1/devices/{device_id}", tags=["devices"], dependencies=[Depends(require("devices.read"))])
async def get_device(request: Request, device_id: str) -> dict[str, Any]:
    """Get a device."""
    store = _store(request)
    async with store.lock:
        return serialize_device(_need_device(store, device_id))


@router.patch("/api/v1/devices/{device_id}", tags=["devices"], dependencies=[Depends(require("devices.write"))])
async def rename_device(request: Request, device_id: str, payload: DeviceRename) -> dict[str, Any]:
    """Rename a device."""
    store = _store(request)
    async with store.lock:
        device = _need_device(store, device_id)
        if any(d["name"] == payload.name and d["id"] != device_id for d in store.devices_of(device["user_id"])):
            raise ApiError(409, "CONFLICT", f"device name {payload.name} already exists for this user")
        device.update(name=payload.name, updated_at=store.now_iso())
        return serialize_device(device)


@router.delete("/api/v1/devices/{device_id}", tags=["devices"], dependencies=[Depends(require("devices.write"))])
async def delete_device(request: Request, device_id: str) -> dict[str, Any]:
    """Delete a device (IP released; peer removed)."""
    store = _store(request)
    async with store.lock:
        _need_device(store, device_id)  # raises 404 when the id is unknown
        store.devices.pop(device_id, None)
        store.device_secrets.pop(device_id, None)
        return {"id": device_id, "deleted": True}


@router.post("/api/v1/devices/{device_id}/enable", tags=["devices"], dependencies=[Depends(require("devices.write"))])
async def enable_device(request: Request, device_id: str) -> dict[str, Any]:
    """Enable a device."""
    store = _store(request)
    async with store.lock:
        device = _need_device(store, device_id)
        device.update(enabled=True, updated_at=store.now_iso())
        return serialize_device(device)


@router.post("/api/v1/devices/{device_id}/disable", tags=["devices"], dependencies=[Depends(require("devices.write"))])
async def disable_device(request: Request, device_id: str) -> dict[str, Any]:
    """Disable a device."""
    store = _store(request)
    async with store.lock:
        device = _need_device(store, device_id)
        device.update(enabled=False, updated_at=store.now_iso())
        return serialize_device(device)


@router.post(
    "/api/v1/devices/{device_id}/regenerate", tags=["devices"], dependencies=[Depends(require("devices.write"))]
)
async def regenerate_device(
    request: Request, device_id: str, payload: DeviceRegenerate | None = Body(default=None)
) -> dict[str, Any]:
    """Regenerate device keys (peer key revoked by reconciliation)."""
    store = _store(request)
    async with store.lock:
        device = _need_device(store, device_id)
        secrets_map = store.device_secrets.setdefault(device_id, {})
        secrets_map["private_key"] = new_wg_key()
        if payload is not None and payload.preshared_key is not None:
            secrets_map["preshared_key"] = new_wg_key() if payload.preshared_key else ""
        device.update(public_key=new_wg_key(), updated_at=store.now_iso())
        return serialize_device(device)


# --- config / QR ------------------------------------------------------------ #
@router.get("/api/v1/devices/{device_id}/config", tags=["config"], dependencies=[Depends(require("configs.read"))])
async def device_config(request: Request, device_id: str, format: str | None = None) -> Response:
    """Download the client configuration (text), or ``?format=json`` for tests."""
    store = _store(request)
    async with store.lock:
        device = _need_device(store, device_id)
        user = _need_user(store, device["user_id"])
        iface = store.interfaces.get(device["interface_id"]) or next(iter(store.interfaces.values()), None)
        if iface is None:
            raise ApiError(409, "NODE_UNAVAILABLE", "device has no tunnel interface")
        secrets_map = store.device_secrets.get(device_id, {})
        text = _config_text(
            store,
            user,
            device,
            iface,
            secrets_map.get("private_key") or new_wg_key(),
            secrets_map.get("preshared_key", ""),
        )
        headers = {
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{_config_stem(store, user, device)}.conf"',
        }
        if format == "json":
            return _json_response(200, {"config": text}, headers)
        return Response(content=text, media_type="text/plain", headers=headers)


@router.get("/api/v1/devices/{device_id}/qr", tags=["config"], dependencies=[Depends(require("configs.read"))])
async def device_qr(request: Request, device_id: str) -> Response:
    """Client configuration as a PNG QR code (exact canonical config bytes)."""
    store = _store(request)
    async with store.lock:
        device = _need_device(store, device_id)
        user = _need_user(store, device["user_id"])
        iface = store.interfaces.get(device["interface_id"]) or next(iter(store.interfaces.values()), None)
        if iface is None:
            raise ApiError(409, "NODE_UNAVAILABLE", "device has no tunnel interface")
        secrets_map = store.device_secrets.get(device_id, {})
        text = _config_text(
            store,
            user,
            device,
            iface,
            secrets_map.get("private_key") or new_wg_key(),
            secrets_map.get("preshared_key", ""),
        )
        stem = _config_stem(store, user, device)
    return Response(
        content=_png_bytes(_qr_matrix(text)),
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{stem}.png"',
        },
    )


# --- statistics ------------------------------------------------------------- #
@router.get("/api/v1/stats", tags=["stats"], dependencies=[Depends(require("stats.read"))])
async def get_stats(request: Request) -> dict[str, Any]:
    """Node-wide statistics."""
    store = _store(request)
    async with store.lock:
        counts = _counts(store)
        return {
            "users_total": counts["users_total"],
            "users_active": counts["users_active"],
            "users_blocked": sum(
                serialize_user(user, store.now())["status"] in ("traffic_exceeded", "expired", "disabled", "suspended")
                for user in store.users.values()
            ),
            "users_deleted": counts["users_deleted"],
            "devices": counts["devices_total"],
            "traffic_used_rx": sum(user["traffic_used_rx"] for user in store.users.values()),
            "traffic_used_tx": sum(user["traffic_used_tx"] for user in store.users.values()),
            "traffic_used_total": sum(user["traffic_used_total"] for user in store.users.values()),
        }


@router.get("/api/v1/users/{user_id}/stats", tags=["stats"], dependencies=[Depends(require("stats.read"))])
async def get_user_stats(request: Request, user_id: str) -> dict[str, Any]:
    """Per-user statistics (used, limit, remaining, percent)."""
    store = _store(request)
    async with store.lock:
        user = serialize_user(_need_user(store, user_id), store.now())
        devices = store.devices_of(user_id)
        used = user["traffic_used_total"]
        limit = user["traffic_limit_bytes"]
        result = {
            "devices": len(devices),
            "traffic_used_rx": user["traffic_used_rx"],
            "traffic_used_tx": user["traffic_used_tx"],
            "traffic_used_total": used,
            "traffic_limit_bytes": limit,
            "last_activity_at": user["last_activity_at"],
        }
        if limit is not None:
            result["traffic_remaining_bytes"] = max(0, limit - used)
            if limit > 0:
                result["traffic_percent_used"] = used * 100.0 / limit
        return result


@router.get("/api/v1/devices/{device_id}/stats", tags=["stats"], dependencies=[Depends(require("stats.read"))])
async def get_device_stats(request: Request, device_id: str) -> dict[str, Any]:
    """Per-device statistics (totals, handshake, online)."""
    store = _store(request)
    async with store.lock:
        device = _need_device(store, device_id)
        now = store.now()
        rx, tx = int(device.get("rx_bytes", 0)), int(device.get("tx_bytes", 0))
        result = {
            "rx_bytes": rx,
            "tx_bytes": tx,
            "last_handshake_at": device["last_handshake_at"],
            "last_endpoint": device["last_endpoint"],
        }
        if device["last_handshake_at"]:
            result["online_window_seconds"] = 180
            result["online"] = _is_online(device["last_handshake_at"], now)
        return result


# --- plans ------------------------------------------------------------------ #
@router.get("/api/v1/templates", tags=["plans"], dependencies=[Depends(require("templates.read"))])
async def list_plans(request: Request) -> dict[str, Any]:
    """List plans."""
    store = _store(request)
    async with store.lock:
        return {"items": [dict(plan) for plan in store.plans.values()]}


@router.post("/api/v1/templates", tags=["plans"], status_code=201, dependencies=[Depends(require("templates.write"))])
async def create_plan(request: Request, payload: PlanWrite) -> dict[str, Any]:
    """Create a plan."""
    store = _store(request)
    async with store.lock:
        if payload.interface_id:
            _need_interface(store, payload.interface_id)
        plan = build_plan(payload.model_dump(), now=store.now())
        store.plans[plan["id"]] = plan
        return plan


@router.get("/api/v1/templates/{template_id}", tags=["plans"], dependencies=[Depends(require("templates.read"))])
async def get_plan(request: Request, template_id: str) -> dict[str, Any]:
    """Get a plan."""
    store = _store(request)
    async with store.lock:
        return dict(_need_plan(store, template_id))


@router.patch("/api/v1/templates/{template_id}", tags=["plans"], dependencies=[Depends(require("templates.write"))])
async def update_plan(request: Request, template_id: str, payload: PlanUpdate) -> dict[str, Any]:
    """Update a plan."""
    store = _store(request)
    patch = payload.model_dump(exclude_unset=True)
    if patch.get("duration_seconds") is None:
        patch.pop("duration_seconds", None)
    async with store.lock:
        plan = _need_plan(store, template_id)
        if patch.get("interface_id"):
            _need_interface(store, patch["interface_id"])
        plan.update({key: value for key, value in patch.items() if key in plan})
        plan["updated_at"] = store.now_iso()
        return dict(plan)


@router.delete("/api/v1/templates/{template_id}", tags=["plans"], dependencies=[Depends(require("templates.write"))])
async def delete_plan(request: Request, template_id: str) -> dict[str, Any]:
    """Delete a plan (refused while assigned to users)."""
    store = _store(request)
    async with store.lock:
        _need_plan(store, template_id)
        assigned = sum(
            1 for user in store.users.values() if user.get("template_id") == template_id and not user.get("deleted")
        )
        if assigned:
            raise ApiError(409, "CONFLICT", f"plan {template_id} is assigned to {assigned} user(s)")
        store.plans.pop(template_id, None)
        return {"id": template_id, "deleted": True}


# --- interfaces ------------------------------------------------------------- #
@router.get("/api/v1/interfaces", tags=["interfaces"], dependencies=[Depends(require("interfaces.read"))])
async def list_interfaces(request: Request) -> dict[str, Any]:
    """List tunnel interfaces/profiles."""
    store = _store(request)
    async with store.lock:
        return {"items": [dict(iface) for iface in store.interfaces.values()]}


@router.post(
    "/api/v1/interfaces", tags=["interfaces"], status_code=201, dependencies=[Depends(require("interfaces.write"))]
)
async def create_interface(request: Request, payload: InterfaceCreate) -> dict[str, Any]:
    """Create a profile (server keypair generated)."""
    store = _store(request)
    if payload.preset is not None and payload.obfuscation is not None:
        raise ApiError(400, "INVALID_REQUEST", "preset and obfuscation are mutually exclusive")
    async with store.lock:
        if any(iface["name"] == payload.name for iface in store.interfaces.values()):
            raise ApiError(409, "CONFLICT", f"interface name {payload.name} already exists")
        iface = build_interface(payload.model_dump(exclude_none=True), now=store.now())
        store.interfaces[iface["id"]] = iface
        return iface


@router.get(
    "/api/v1/interfaces/{interface_id}", tags=["interfaces"], dependencies=[Depends(require("interfaces.read"))]
)
async def get_interface(request: Request, interface_id: str) -> dict[str, Any]:
    """Get a profile."""
    store = _store(request)
    async with store.lock:
        return dict(_need_interface(store, interface_id))


@router.patch(
    "/api/v1/interfaces/{interface_id}", tags=["interfaces"], dependencies=[Depends(require("interfaces.write"))]
)
async def update_interface(request: Request, interface_id: str, payload: InterfaceUpdate) -> dict[str, Any]:
    """Update MTU/endpoint/obfuscation/enabled (name, port and pool are immutable)."""
    store = _store(request)
    patch = payload.model_dump(exclude_unset=True)
    async with store.lock:
        iface = _need_interface(store, interface_id)
        if "mtu" in patch and patch["mtu"] is not None:
            iface["mtu"] = int(patch["mtu"])
        if "enabled" in patch and patch["enabled"] is not None:
            iface["enabled"] = bool(patch["enabled"])
        if "endpoint_override" in patch:
            iface["endpoint_override"] = patch["endpoint_override"] or f"vpn.mock.local:{iface['listen_port']}"
        if patch.get("obfuscation") is not None:
            iface["obfuscation"] = {**DEFAULT_OBFUSCATION, **patch["obfuscation"]}
            iface["preset"] = "custom"
        iface["updated_at"] = store.now_iso()
        return dict(iface)


@router.delete(
    "/api/v1/interfaces/{interface_id}", tags=["interfaces"], dependencies=[Depends(require("interfaces.write"))]
)
async def delete_interface(request: Request, interface_id: str) -> dict[str, Any]:
    """Delete a profile (refused while devices exist)."""
    store = _store(request)
    async with store.lock:
        _need_interface(store, interface_id)
        attached = sum(1 for device in store.devices.values() if device["interface_id"] == interface_id)
        if attached:
            raise ApiError(409, "CONFLICT", f"interface {interface_id} still has {attached} device(s)")
        store.interfaces.pop(interface_id, None)
        return {"id": interface_id, "deleted": True}


# --- settings --------------------------------------------------------------- #
@router.get("/api/v1/settings", tags=["settings"], dependencies=[Depends(require("node.read"))])
async def get_settings(request: Request) -> dict[str, Any]:
    """List settings (secrets redacted — only ``secret_set``)."""
    store = _store(request)
    async with store.lock:
        return _settings_catalog(store)


@router.patch("/api/v1/settings", tags=["settings"], dependencies=[Depends(require("node.settings"))])
async def patch_settings(request: Request, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Update settings (typed registry; validated)."""
    store = _store(request)
    async with store.lock:
        for key, value in payload.items():
            current = store.settings.get(key)
            if current is not None and value is not None and not isinstance(value, type(current)):
                raise ApiError(400, "INVALID_REQUEST", f"setting {key} expects {type(current).__name__}")
        store.settings.update(payload)
        return {"updated": sorted(payload), **_settings_catalog(store)}


# --- webhooks --------------------------------------------------------------- #
@router.get("/api/v1/webhooks", tags=["webhooks"], dependencies=[Depends(require("webhooks.read"))])
async def list_webhooks(request: Request) -> dict[str, Any]:
    """List webhook endpoints (with delivery stats)."""
    store = _store(request)
    async with store.lock:
        return {"items": [_serialize_webhook(store, hook) for hook in store.webhooks.values()]}


@router.post("/api/v1/webhooks", tags=["webhooks"], status_code=201, dependencies=[Depends(require("webhooks.write"))])
async def create_webhook(request: Request, payload: WebhookCreate) -> dict[str, Any]:
    """Create a webhook endpoint (an empty ``secret`` generates one, returned once)."""
    store = _store(request)
    events = _validate_events(payload.events)
    async with store.lock:
        webhook = {
            "id": new_id("wh"),
            "url": payload.url,
            "enabled": True,
            "events": events,
            "include_reseller_events": bool(payload.include_reseller_events),
            "created_at": store.now_iso(),
        }
        generated = payload.secret is None or payload.secret == ""
        store.webhooks[webhook["id"]] = webhook
        store.webhook_secrets[webhook["id"]] = secrets.token_urlsafe(32) if generated else str(payload.secret)
        store.deliveries[webhook["id"]] = [_new_receipt(store, events[0])]
        body = _serialize_webhook(store, webhook)
        if generated:
            body["secret"] = store.webhook_secrets[webhook["id"]]
        return body


@router.get("/api/v1/webhooks/{webhook_id}", tags=["webhooks"], dependencies=[Depends(require("webhooks.read"))])
async def get_webhook(request: Request, webhook_id: str) -> dict[str, Any]:
    """Get a webhook endpoint."""
    store = _store(request)
    async with store.lock:
        return _serialize_webhook(store, _need_webhook(store, webhook_id))


@router.patch("/api/v1/webhooks/{webhook_id}", tags=["webhooks"], dependencies=[Depends(require("webhooks.write"))])
async def update_webhook(request: Request, webhook_id: str, payload: WebhookUpdate) -> dict[str, Any]:
    """Update url/events/enabled or rotate the secret."""
    store = _store(request)
    patch = payload.model_dump(exclude_unset=True)
    rotate = patch.pop("secret", None)
    body: dict[str, Any] | None = None
    async with store.lock:
        webhook = _need_webhook(store, webhook_id)
        if patch.get("events") is not None:
            webhook["events"] = _validate_events(patch["events"])
        for key in ("url", "enabled", "include_reseller_events"):
            if patch.get(key) is not None:
                webhook[key] = patch[key]
        if rotate is not None:
            store.webhook_secrets[webhook_id] = secrets.token_urlsafe(32) if rotate == "" else str(rotate)
            body = {"secret": store.webhook_secrets[webhook_id], **_serialize_webhook(store, webhook)}
        return body or _serialize_webhook(store, webhook)


@router.delete("/api/v1/webhooks/{webhook_id}", tags=["webhooks"], dependencies=[Depends(require("webhooks.write"))])
async def delete_webhook(request: Request, webhook_id: str) -> dict[str, Any]:
    """Delete a webhook endpoint."""
    store = _store(request)
    async with store.lock:
        _need_webhook(store, webhook_id)
        store.webhooks.pop(webhook_id, None)
        store.webhook_secrets.pop(webhook_id, None)
        store.deliveries.pop(webhook_id, None)
        return {"id": webhook_id, "deleted": True}


@router.post(
    "/api/v1/webhooks/{webhook_id}/redeliver", tags=["webhooks"], dependencies=[Depends(require("webhooks.write"))]
)
async def redeliver_webhook(request: Request, webhook_id: str, payload: RedeliverRequest) -> Response:
    """Manually redeliver one delivery (resets attempts)."""
    store = _store(request)
    async with store.lock:
        _need_webhook(store, webhook_id)
        receipt = store.lookup_delivery(webhook_id, payload.delivery_id)
        if receipt is None:
            raise ApiError(404, "DELIVERY_NOT_FOUND", f"delivery {payload.delivery_id} not found")
        receipt.update(status="pending", attempts=0, next_attempt_at=store.now_iso(), updated_at=store.now_iso())
        return _json_response(202, {"delivery_id": receipt["id"], "status": receipt["status"]})


@router.get(
    "/api/v1/webhooks/{webhook_id}/deliveries", tags=["webhooks"], dependencies=[Depends(require("webhooks.read"))]
)
async def list_deliveries(
    request: Request, webhook_id: str, limit: int = 50, before: str | None = None, event_id: str | None = None
) -> dict[str, Any]:
    """List non-secret delivery receipts for one owned endpoint (newest first)."""
    store = _store(request)
    async with store.lock:
        _need_webhook(store, webhook_id)
        receipts = list(reversed(store.deliveries.get(webhook_id, [])))
        if event_id:
            receipts = [receipt for receipt in receipts if receipt["event_id"] == event_id]
        if before:
            ids = [receipt["id"] for receipt in receipts]
            if before in ids:
                receipts = receipts[ids.index(before) + 1 :]
        page = receipts[: max(1, min(100, limit))]
        return {"items": page, "next_before": page[-1]["id"] if page else ""}


@router.get(
    "/api/v1/webhooks/{webhook_id}/deliveries/{delivery_id}",
    tags=["webhooks"],
    dependencies=[Depends(require("webhooks.read"))],
)
async def get_delivery(request: Request, webhook_id: str, delivery_id: str) -> dict[str, Any]:
    """Get one non-secret delivery receipt."""
    store = _store(request)
    async with store.lock:
        _need_webhook(store, webhook_id)
        receipt = store.lookup_delivery(webhook_id, delivery_id)
        if receipt is None:
            raise ApiError(404, "DELIVERY_NOT_FOUND", f"delivery {delivery_id} not found")
        return dict(receipt)


# --------------------------------------------------------------------------- #
# test affordances — NOT part of the WG-Guard contract
# --------------------------------------------------------------------------- #
@router.post("/__mock__/reset", tags=["__mock__"])
async def mock_reset(request: Request) -> dict[str, Any]:
    """Wipe all state back to the seed (including the request journal)."""
    store = _store(request)
    async with store.lock:
        store.reset()
    LOGGER.info("mock state reset")
    return {"ok": True, "reset": True}


@router.get("/__mock__/requests", tags=["__mock__"])
async def mock_requests(request: Request) -> list[dict[str, Any]]:
    """Every request received so far, in arrival order."""
    store = _store(request)
    async with store.lock:
        return [dict(entry) for entry in store.requests]


@router.post("/__mock__/fail", tags=["__mock__"])
async def mock_fail(request: Request, payload: MockFailRequest) -> dict[str, Any]:
    """Make the next ``count`` matching requests fail with ``status``."""
    store = _store(request)
    async with store.lock:
        store.failures.append(
            FailureRule(path=payload.path, status_code=payload.status, body=payload.body, remaining=payload.count)
        )
        return {"armed": True, "path": payload.path, "status": payload.status, "count": payload.count}


@router.post("/__mock__/slow", tags=["__mock__"])
async def mock_slow(request: Request, payload: MockSlowRequest) -> dict[str, Any]:
    """Delay the next ``count`` matching requests by ``seconds``."""
    store = _store(request)
    async with store.lock:
        store.slows.append(SlowRule(path=payload.path, seconds=payload.seconds, remaining=payload.count))
        return {"armed": True, "path": payload.path, "seconds": payload.seconds, "count": payload.count}


@router.post("/__mock__/seed/plan", tags=["__mock__"])
async def mock_seed_plan(request: Request, payload: MockSeedPlan) -> dict[str, Any]:
    """Add one plan to the catalog."""
    store = _store(request)
    async with store.lock:
        plan = build_plan(payload.model_dump(exclude_none=True), template_id=payload.id, now=store.now())
        if plan["id"] in store.plans:
            raise ApiError(409, "CONFLICT", f"plan {plan['id']} already exists")
        store.plans[plan["id"]] = plan
        return plan


@router.post("/__mock__/expire", tags=["__mock__"])
async def mock_expire(request: Request, payload: MockExpireRequest) -> dict[str, Any]:
    """Force-expire a user so it reports ``status: "expired"`` and ``enabled: false``."""
    store = _store(request)
    async with store.lock:
        user = store.users.get(payload.user_id or "")
        if user is None and payload.username:
            user = next((u for u in store.users.values() if u["username"] == payload.username), None)
        if user is None:
            raise ApiError(404, "USER_NOT_FOUND", "user not found for /__mock__/expire")
        user.update(
            expires_at=iso(store.now() - timedelta(seconds=1)),
            enabled=False,
            disable_reason="expired",
            updated_at=store.now_iso(),
        )
        return serialize_user(user, store.now())


# --------------------------------------------------------------------------- #
# middleware, error handlers, factory
# --------------------------------------------------------------------------- #
async def _drain(receive: Callable[[], Awaitable[dict[str, Any]]]) -> bytes:
    """Read the whole request body so it can be journaled and replayed."""
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body", b""))
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


def _decode_body(body: bytes) -> Any:
    """Decode a request body for the journal (JSON when possible)."""
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body.decode("utf-8", "replace")[:4096]


def _with_request_id(payload: Any, request_id: str) -> Any:
    """Fill in missing envelope fields on an injected error body."""
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        error = {**payload["error"]}
        error.setdefault("code", "NODE_UNAVAILABLE")
        error.setdefault("message", "injected failure")
        error.setdefault("request_id", request_id)
        return {**payload, "error": error}
    return payload


async def _send_json(
    send: Callable[[dict[str, Any]], Awaitable[None]], status_code: int, payload: Any, request_id: str
) -> None:
    """Send a complete JSON response from middleware."""
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-request-id", request_id.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class MockMiddleware:
    """Pure-ASGI middleware: request journal, fault injection, request ids."""

    def __init__(self, app: Any, store: MockStore) -> None:
        self.app = app
        self.store = store

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        body = await _drain(receive)
        request_id = f"req_{secrets.token_hex(8)}"
        scope.setdefault("state", {})["request_id"] = request_id
        path = scope.get("path", "")
        async with self.store.lock:
            self.store.record_request(
                {
                    "method": scope.get("method", ""),
                    "path": path,
                    "query": scope.get("query_string", b"").decode("latin-1"),
                    "headers": {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])},
                    "body": _decode_body(body),
                }
            )
            failure = self.store.take_failure(path)
            delay = self.store.take_delay(path)
        if delay > 0:
            LOGGER.info("delaying %s by %.3fs", path, delay)
            await asyncio.sleep(delay)
        if failure is not None:
            LOGGER.warning("injecting %s for %s", failure.status_code, path)
            payload = (
                failure.body
                if failure.body is not None
                else _envelope("NODE_UNAVAILABLE", "injected failure", request_id)
            )
            await _send_json(send, failure.status_code, _with_request_id(payload, request_id), request_id)
            return
        delivered = False

        async def replay() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def send_with_id(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = {
                    **message,
                    "headers": [*message.get("headers", []), (b"x-request-id", request_id.encode("ascii"))],
                }
            await send(message)

        await self.app(scope, replay, send_with_id)


def _handle_api_error(request: Request, exc: Exception) -> JSONResponse:
    """Render :class:`ApiError` as the documented envelope."""
    assert isinstance(exc, ApiError)
    return _error_response(exc.status_code, exc.code, exc.message, _request_id(request))


def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    """Map request validation failures onto 400 ``INVALID_REQUEST``."""
    errors = exc.errors() if isinstance(exc, RequestValidationError) else []
    first = errors[0] if errors else {}
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = f"{location}: {first.get('msg', 'invalid request')}" if location else "invalid request"
    return _error_response(400, "INVALID_REQUEST", message, _request_id(request))


def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    """Render framework 404/405/... as the documented envelope."""
    status_code = getattr(exc, "status_code", 500)
    code = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}.get(status_code, f"HTTP_{status_code}")
    return _error_response(status_code, code, str(getattr(exc, "detail", "request failed")), _request_id(request))


def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler so tests always receive the documented envelope."""
    LOGGER.exception("unhandled error on %s %s", request.method, request.url.path)
    return _error_response(500, "INTERNAL_ERROR", "internal error", _request_id(request))


def create_app(config: MockConfig | None = None) -> FastAPI:
    """Build a fresh, isolated mock WG-Guard panel.

    Args:
        config: typed mock configuration; :meth:`MockConfig.default` when omitted.

    Returns:
        A FastAPI application whose state is entirely in memory.
    """
    store = MockStore(config)
    app = FastAPI(
        title="WG-Guard Mock Panel",
        version=store.config.version,
        description="In-memory test double of the WG-Guard panel REST API — not the real panel.",
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.store = store
    app.add_middleware(MockMiddleware, store=store)
    app.add_exception_handler(ApiError, _handle_api_error)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
    app.include_router(router)
    LOGGER.info(
        "mock WG-Guard panel ready: %d token(s), %d plan(s), %d interface(s)",
        len(store.config.tokens),
        len(store.plans),
        len(store.interfaces),
    )
    return app
