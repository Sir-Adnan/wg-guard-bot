"""Async REST client for a WG-Guard node (``/api/v1``).

Design goals
------------
* **Contract fidelity** — mirrors ``docs/upstream-api/openapi-wg-guard.json``
  1:1, including the error envelope and cursor pagination.
* **Safe retries** — network errors / 5xx / 429 are retried with exponential
  backoff *only* for safe requests (``GET``) or when the caller supplied an
  ``Idempotency-Key``.  A blind retry of a mutation could double-provision.
* **Testability** — an ``httpx`` transport can be injected, so the whole client
  runs against the in-repo mock panel without a network.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import suppress
from types import TracebackType
from typing import Any, Self

import httpx

from app.core.errors import (
    PanelAuthError,
    PanelConflict,
    PanelError,
    PanelNotFound,
    PanelUnavailable,
    PanelValidation,
)
from app.core.logging import get_logger
from app.panels.schemas import (
    CustomerLink,
    CustomerLinkRotation,
    Device,
    Interface,
    NextPlan,
    NextPlanActivation,
    Node,
    OperationResult,
    Plan,
    PlanPatch,
    Status,
    Telemetry,
    User,
    UserCreate,
    UserPage,
    UserPatch,
    WebhookEndpoint,
)

log = get_logger(__name__)

#: Statuses worth retrying (transient by nature).
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


class WGGuardClient:
    """Thin, typed wrapper around one node's REST surface."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        max_backoff: float = 8.0,
        transport: httpx.AsyncBaseTransport | None = None,
        verify: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_retries = max(0, max_retries)
        self.backoff_base = backoff_base
        self.max_backoff = max_backoff
        self._timeout = httpx.Timeout(timeout, connect=connect_timeout)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self._timeout,
            transport=transport,
            verify=verify,
            follow_redirects=False,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "wg-guard-bot/1.0 (+https://github.com/)",
            },
        )

    # -- lifecycle ---------------------------------------------------------
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- core request ------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        expect: str = "json",
        retry: bool | None = None,
    ) -> Any:
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        safe = method.upper() in ("GET", "HEAD", "OPTIONS")
        may_retry = retry if retry is not None else (safe or bool(idempotency_key))

        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await self._client.request(
                    method, path, json=json, params=clean_params or None, headers=headers
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout) as exc:
                last_error = exc
                if not may_retry or attempt >= self.max_retries:
                    raise PanelUnavailable(f"ارتباط با پنل برقرار نشد ({type(exc).__name__}).") from exc
                await self._sleep(attempt)
                continue
            except httpx.HTTPError as exc:
                raise PanelError(f"خطای انتقال در پنل: {exc}") from exc

            if response.status_code in RETRY_STATUSES and may_retry and attempt < self.max_retries:
                await self._sleep(attempt, response.headers.get("Retry-After"))
                continue

            return self._handle(response, expect=expect)

        raise PanelUnavailable("پنل پس از چند تلاش پاسخ نداد.") from last_error

    async def _sleep(self, attempt: int, retry_after: str | None = None) -> None:
        delay = min(self.backoff_base * (2**attempt), self.max_backoff)
        delay += random.uniform(0, delay * 0.1)  # jitter
        if retry_after:
            with suppress(ValueError):
                delay = max(delay, min(float(retry_after), self.max_backoff))
        log.debug("Retrying panel request in %.2fs (attempt %d)", delay, attempt + 1)
        await asyncio.sleep(delay)

    def _handle(self, response: httpx.Response, *, expect: str) -> Any:
        if response.status_code >= 400:
            raise self._to_error(response)

        if expect == "text":
            return response.text
        if expect == "bytes":
            return response.content
        if expect == "none" or response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise PanelError("پاسخ پنل قابل خواندن نبود.") from exc

    @staticmethod
    def _to_error(response: httpx.Response) -> PanelError:
        code: str | None = None
        message: str | None = None
        payload: Any = None
        try:
            payload = response.json()
        except ValueError:
            payload = None

        if isinstance(payload, dict):
            envelope = payload.get("error")
            if isinstance(envelope, dict):
                code = envelope.get("code")
                message = envelope.get("message")

        detail = f"{code}: {message}" if code else (message or response.text[:200])
        status = response.status_code

        if status in (401, 403):
            return PanelAuthError(f"دسترسی پنل رد شد ({detail or status}).", status=status, panel_code=code)
        if status == 409:
            return PanelConflict(f"پنل این عملیات را نپذیرفت ({detail or status}).", status=status, panel_code=code)
        if status == 404:
            return PanelNotFound(f"در پنل یافت نشد ({detail or status}).", status=status, panel_code=code)
        if status == 400:
            return PanelValidation(f"درخواست نامعتبر برای پنل ({detail or status}).", status=status, panel_code=code)
        if status in RETRY_STATUSES:
            return PanelUnavailable(f"پنل در دسترس نیست ({detail or status}).", status=status, panel_code=code)
        return PanelError(f"خطای پنل ({detail or status}).", status=status, panel_code=code)

    # -- ops ---------------------------------------------------------------
    async def health(self) -> Status:
        data = await self._request("GET", "/healthz", retry=True)
        return Status.model_validate(data or {})

    async def ready(self) -> bool:
        try:
            response = await self._client.get("/readyz")
        except httpx.HTTPError:
            return False
        return response.status_code < 400

    async def node(self) -> Node:
        return Node.model_validate(await self._request("GET", "/api/v1/node"))

    async def node_health(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/node/health", retry=True)

    async def node_stats(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/node/stats")

    async def telemetry(self, points: int = 60) -> Telemetry:
        data = await self._request("GET", "/api/v1/node/telemetry", params={"points": points})
        return Telemetry.model_validate(data)

    async def stats(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/stats")

    # -- integration -------------------------------------------------------
    async def create_purchase(
        self,
        plan_id: str,
        *,
        idempotency_key: str,
        username: str | None = None,
        device_name: str = "device-1",
    ) -> OperationResult:
        """Atomic purchase: commits user + first device + customer link."""
        payload: dict[str, Any] = {"plan_id": plan_id, "device_name": device_name}
        if username:
            payload["username"] = username
        data = await self._request("POST", "/api/v1/purchases", json=payload, idempotency_key=idempotency_key)
        return OperationResult.model_validate(data)

    async def operation_result(self, idempotency_key: str) -> OperationResult:
        data = await self._request("GET", "/api/v1/operations/result", idempotency_key=idempotency_key)
        return OperationResult.model_validate(data)

    async def customer_subscription(self, user_id: str) -> CustomerLink:
        data = await self._request("GET", f"/api/v1/users/{user_id}/subscription")
        return CustomerLink.model_validate(data)

    async def rotate_customer_access(self, user_id: str) -> CustomerLinkRotation:
        data = await self._request("POST", f"/api/v1/users/{user_id}/subscription/rotate")
        return CustomerLinkRotation.model_validate(data)

    # -- next plan ---------------------------------------------------------
    async def get_next_plan(self, user_id: str) -> NextPlan | None:
        data = await self._request("GET", f"/api/v1/users/{user_id}/next-plan")
        plan = (data or {}).get("next_plan")
        return NextPlan.model_validate(plan) if plan else None

    async def put_next_plan(
        self, user_id: str, plan_id: str, *, carry_unused_traffic: bool = False, idempotency_key: str | None = None
    ) -> NextPlan:
        data = await self._request(
            "PUT",
            f"/api/v1/users/{user_id}/next-plan",
            json={"plan_id": plan_id, "carry_unused_traffic": carry_unused_traffic},
            idempotency_key=idempotency_key,
        )
        return NextPlan.model_validate(data)

    async def delete_next_plan(self, user_id: str, *, idempotency_key: str | None = None) -> None:
        await self._request(
            "DELETE", f"/api/v1/users/{user_id}/next-plan", idempotency_key=idempotency_key, expect="none"
        )

    async def list_next_plan_activations(self, user_id: str, limit: int = 20) -> list[NextPlanActivation]:
        data = await self._request("GET", f"/api/v1/users/{user_id}/next-plan/activations", params={"limit": limit})
        return [NextPlanActivation.model_validate(item) for item in (data or {}).get("items", [])]

    # -- users -------------------------------------------------------------
    async def create_user(self, payload: UserCreate | dict[str, Any], *, idempotency_key: str | None = None) -> User:
        body = payload.model_dump(exclude_none=True) if isinstance(payload, UserCreate) else payload
        return User.model_validate(
            await self._request("POST", "/api/v1/users", json=body, idempotency_key=idempotency_key)
        )

    async def list_users(
        self,
        *,
        limit: int = 50,
        cursor: str | None = None,
        sort: str | None = None,
        order: str | None = None,
        **filters: Any,
    ) -> UserPage:
        params: dict[str, Any] = {"limit": min(limit, 500), "cursor": cursor, "sort": sort, "order": order}
        params.update(filters)
        data = await self._request("GET", "/api/v1/users", params=params)
        return UserPage.model_validate(data)

    async def iter_users(self, *, page_size: int = 100, **filters: Any) -> AsyncIterator[User]:
        """Walk every page of users lazily."""
        cursor: str | None = None
        while True:
            page = await self.list_users(limit=page_size, cursor=cursor, **filters)
            for user in page.items:
                yield user
            if not page.has_more:
                return
            cursor = page.next_cursor

    async def get_user(self, user_id: str) -> User:
        return User.model_validate(await self._request("GET", f"/api/v1/users/{user_id}"))

    async def update_user(self, user_id: str, patch: UserPatch | dict[str, Any]) -> User:
        body = patch.model_dump(exclude_unset=True) if isinstance(patch, UserPatch) else patch
        return User.model_validate(await self._request("PATCH", f"/api/v1/users/{user_id}", json=body))

    async def delete_user(self, user_id: str) -> None:
        await self._request("DELETE", f"/api/v1/users/{user_id}", expect="none")

    async def enable_user(self, user_id: str) -> User:
        return User.model_validate(await self._request("POST", f"/api/v1/users/{user_id}/enable"))

    async def disable_user(self, user_id: str, reason: str = "manual") -> User:
        return User.model_validate(
            await self._request("POST", f"/api/v1/users/{user_id}/disable", json={"reason": reason})
        )

    async def renew_user(
        self,
        user_id: str,
        *,
        mode: str = "from_expiration",
        duration_seconds: int | None = None,
        exact: str | None = None,
        idempotency_key: str | None = None,
    ) -> User:
        body: dict[str, Any] = {"mode": mode}
        if duration_seconds is not None:
            body["duration_seconds"] = duration_seconds
        if exact is not None:
            body["exact"] = exact
        return User.model_validate(
            await self._request("POST", f"/api/v1/users/{user_id}/renew", json=body, idempotency_key=idempotency_key)
        )

    async def user_stats(self, user_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/users/{user_id}/stats")

    # -- traffic -----------------------------------------------------------
    async def add_traffic(
        self, user_id: str, *, rx_bytes: int = 0, tx_bytes: int = 0, idempotency_key: str | None = None
    ) -> User:
        return User.model_validate(
            await self._request(
                "POST",
                f"/api/v1/users/{user_id}/traffic/add",
                json={"rx_bytes": rx_bytes, "tx_bytes": tx_bytes},
                idempotency_key=idempotency_key,
            )
        )

    async def set_traffic(
        self,
        user_id: str,
        *,
        rx_bytes: int | None = None,
        tx_bytes: int | None = None,
        idempotency_key: str | None = None,
    ) -> User:
        return User.model_validate(
            await self._request(
                "POST",
                f"/api/v1/users/{user_id}/traffic/set",
                json={"rx_bytes": rx_bytes, "tx_bytes": tx_bytes},
                idempotency_key=idempotency_key,
            )
        )

    async def reset_traffic(self, user_id: str, *, idempotency_key: str | None = None) -> User:
        return User.model_validate(
            await self._request("POST", f"/api/v1/users/{user_id}/traffic/reset", idempotency_key=idempotency_key)
        )

    async def user_traffic_series(
        self, user_id: str, *, granularity: str = "hourly", hours: int = 48
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/api/v1/users/{user_id}/traffic",
            params={"granularity": granularity, "hours": hours},
        )

    # -- devices -----------------------------------------------------------
    async def list_devices(self, user_id: str) -> list[Device]:
        data = await self._request("GET", f"/api/v1/users/{user_id}/devices")
        return [Device.model_validate(item) for item in (data or {}).get("items", [])]

    async def create_device(
        self, user_id: str, *, name: str, interface_id: str | None = None, preshared_key: bool = False
    ) -> Device:
        body: dict[str, Any] = {"name": name, "preshared_key": preshared_key}
        if interface_id:
            body["interface_id"] = interface_id
        return Device.model_validate(await self._request("POST", f"/api/v1/users/{user_id}/devices", json=body))

    async def get_device(self, device_id: str) -> Device:
        return Device.model_validate(await self._request("GET", f"/api/v1/devices/{device_id}"))

    async def rename_device(self, device_id: str, name: str) -> Device:
        return Device.model_validate(await self._request("PATCH", f"/api/v1/devices/{device_id}", json={"name": name}))

    async def delete_device(self, device_id: str) -> None:
        await self._request("DELETE", f"/api/v1/devices/{device_id}", expect="none")

    async def enable_device(self, device_id: str) -> None:
        await self._request("POST", f"/api/v1/devices/{device_id}/enable", expect="none")

    async def disable_device(self, device_id: str) -> None:
        await self._request("POST", f"/api/v1/devices/{device_id}/disable", expect="none")

    async def regenerate_device(self, device_id: str, *, preshared_key: bool | None = None) -> Device:
        body = {"preshared_key": preshared_key} if preshared_key is not None else {}
        # Not idempotent per the contract: the caller must opt into retries.
        return Device.model_validate(
            await self._request("POST", f"/api/v1/devices/{device_id}/regenerate", json=body or None, retry=False)
        )

    async def device_config(self, device_id: str) -> str:
        """Canonical AWG client configuration (contains a private key)."""
        return await self._request("GET", f"/api/v1/devices/{device_id}/config", expect="text", retry=True)

    async def device_qr(self, device_id: str) -> bytes:
        return await self._request("GET", f"/api/v1/devices/{device_id}/qr", expect="bytes", retry=True)

    async def device_stats(self, device_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/devices/{device_id}/stats")

    # -- plans -------------------------------------------------------------
    async def list_plans(self) -> list[Plan]:
        data = await self._request("GET", "/api/v1/plans")
        return [Plan.model_validate(item) for item in (data or {}).get("items", [])]

    async def get_plan(self, plan_id: str) -> Plan:
        return Plan.model_validate(await self._request("GET", f"/api/v1/plans/{plan_id}"))

    async def create_plan(self, patch: PlanPatch | dict[str, Any]) -> Plan:
        body = patch.model_dump(exclude_none=True) if isinstance(patch, PlanPatch) else patch
        return Plan.model_validate(await self._request("POST", "/api/v1/plans", json=body))

    async def update_plan(self, plan_id: str, patch: PlanPatch | dict[str, Any]) -> Plan:
        body = patch.model_dump(exclude_none=True) if isinstance(patch, PlanPatch) else patch
        return Plan.model_validate(await self._request("PATCH", f"/api/v1/plans/{plan_id}", json=body))

    async def delete_plan(self, plan_id: str) -> None:
        await self._request("DELETE", f"/api/v1/plans/{plan_id}", expect="none")

    # -- interfaces --------------------------------------------------------
    async def list_interfaces(self) -> list[Interface]:
        data = await self._request("GET", "/api/v1/interfaces")
        return [Interface.model_validate(item) for item in (data or {}).get("items", [])]

    async def get_interface(self, interface_id: str) -> Interface:
        return Interface.model_validate(await self._request("GET", f"/api/v1/interfaces/{interface_id}"))

    async def create_interface(self, payload: dict[str, Any]) -> Interface:
        return Interface.model_validate(await self._request("POST", "/api/v1/interfaces", json=payload))

    async def update_interface(self, interface_id: str, payload: dict[str, Any]) -> Interface:
        return Interface.model_validate(
            await self._request("PATCH", f"/api/v1/interfaces/{interface_id}", json=payload)
        )

    async def delete_interface(self, interface_id: str) -> None:
        await self._request("DELETE", f"/api/v1/interfaces/{interface_id}", expect="none")

    # -- settings & webhooks ----------------------------------------------
    async def get_settings(self) -> dict[str, Any]:
        return await self._request("GET", "/api/v1/settings")

    async def patch_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", "/api/v1/settings", json=values)

    async def list_webhooks(self) -> list[WebhookEndpoint]:
        data = await self._request("GET", "/api/v1/webhooks")
        return [WebhookEndpoint.model_validate(item) for item in (data or {}).get("items", [])]

    async def create_webhook(self, url: str, events: list[str], *, secret: str = "") -> WebhookEndpoint:
        return WebhookEndpoint.model_validate(
            await self._request("POST", "/api/v1/webhooks", json={"url": url, "events": events, "secret": secret})
        )

    async def delete_webhook(self, webhook_id: str) -> None:
        await self._request("DELETE", f"/api/v1/webhooks/{webhook_id}", expect="none")

    async def list_webhook_deliveries(self, webhook_id: str, *, limit: int = 50) -> dict[str, Any]:
        return await self._request("GET", f"/api/v1/webhooks/{webhook_id}/deliveries", params={"limit": limit})

    async def redeliver_webhook(self, webhook_id: str, delivery_id: str) -> None:
        await self._request(
            "POST", f"/api/v1/webhooks/{webhook_id}/redeliver", json={"delivery_id": delivery_id}, expect="none"
        )


__all__ = ["RETRY_STATUSES", "WGGuardClient"]
