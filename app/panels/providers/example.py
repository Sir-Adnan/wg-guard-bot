"""Provider template — copy this file to add a new VPN backend.

Scenario: the shop owner wants to sell from a **PasarGuard** node as well as
from WG-Guard.  Four steps, none of which touch business logic:

1. ``cp app/panels/providers/example.py app/panels/providers/pasarguard.py``
2. implement the methods below against that panel's HTTP API (use
   :mod:`httpx` and put the raw calls in a separate ``client.py`` next to it,
   mirroring ``app/panels/client.py``);
3. set ``kind = "pasarguard"`` and a Persian ``label``;
4. import the module in ``app/panels/providers/__init__.py`` so the
   ``@register`` decorator runs.

That is all: the admin panel's "panel type" dropdown is generated from the
registry, ``Panel.kind`` selects the adapter at runtime, and provisioning,
delivery, renewal, reminders and reporting keep working because they only speak
the canonical models.

Checklist while implementing
----------------------------
* **Map, do not leak.** Return :mod:`app.panels.models` objects.  Keep vendor
  payloads in ``raw``.  Never return a vendor status string — map it to one of
  ``active | waiting_first_connection | disabled | expired | traffic_exceeded``.
* **Declare only what you implement.**  ``capabilities`` drives the UI and lets
  the domain degrade instead of crashing; a partial backend is fine.
* **Be exactly-once where the port demands it.**  ``purchase`` must return the
  same result for the same ``idempotency_key``.  If the vendor has no
  idempotency mechanism, persist a key→result map in your own table — never
  answer "probably".
* **Let ``recover_purchase`` answer honestly.**  ``None`` means "not committed,
  safe to retry".  Returning ``None`` when the purchase *did* commit will
  duplicate a customer's account.
* **Raise :mod:`app.core.errors` types**, not raw ``httpx`` errors, so the
  operator sees a Persian message instead of a traceback.
* **Never swallow ``asyncio.CancelledError``.**

This module is imported for its documentation value; it is registered under
``kind = "example"`` so ``registry.choices()`` can show it in development, and
every method raises ``NotImplementedError`` on purpose.
"""

from __future__ import annotations

from typing import Any

from app.core.errors import PanelError
from app.panels.base import CAP_ATOMIC_PURCHASE, CAP_PURCHASE_RECOVERY, PanelProvider
from app.panels.models import (
    NodeInfo,
    PlanSpec,
    ProviderHealth,
    PurchaseResult,
    RemoteDevice,
    RemoteUser,
    SubscriptionLink,
)
from app.panels.registry import register


@register
class ExampleProvider(PanelProvider):
    """Skeleton adapter.  Replace every body with real HTTP calls."""

    kind = "example"
    label = "نمونه (قالب توسعه)"
    description = "قالب مستند برای افزودن پنل جدید — تا زمانی که پیاده‌سازی نشود غیرفعال است"

    #: Declare only the features you actually implement.  The `CAP_*` constants
    #: live in :mod:`app.panels.base`.
    capabilities = frozenset({CAP_ATOMIC_PURCHASE, CAP_PURCHASE_RECOVERY})

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def aclose(self) -> None:
        """Close the underlying HTTP client.

        Keep a single ``httpx.AsyncClient`` per provider instance — creating one
        per request leaks connections and defeats keep-alive.
        """

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------
    async def health(self) -> ProviderHealth:
        """Cheap probe.  Return ``ProviderHealth(online=False, message=...)``
        instead of raising when the node is simply unreachable — the health job
        records that as an offline panel rather than an exception.
        """
        raise NotImplementedError

    async def node_info(self) -> NodeInfo:
        """Identity, version and the interface list.

        Used by the "check connection" button and by the plan editor's
        interface dropdown.  Return the vendor's own ``kind`` in
        ``NodeInfo.provider_kind``.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Catalogue
    # ------------------------------------------------------------------
    async def ensure_plan(self, spec: PlanSpec, *, existing_ref: str | None = None) -> str:
        """Create or update the node-side plan and return its reference.

        If the vendor has no concept of a plan (for example a plain WireGuard
        panel where every peer is the same), return a sentinel such as
        ``"default"`` and drop :data:`CAP_PLAN_SYNC` from ``capabilities``.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Provisioning
    # ------------------------------------------------------------------
    async def purchase(
        self,
        *,
        plan_ref: str,
        username: str,
        device_name: str,
        idempotency_key: str,
    ) -> PurchaseResult:
        """Create the account, its first peer and its subscription link.

        Must be idempotent on ``idempotency_key``.
        """
        raise NotImplementedError

    async def recover_purchase(self, idempotency_key: str) -> PurchaseResult | None:
        """Return the committed purchase for this key, or ``None`` if none.

        Getting this wrong duplicates customer accounts, so when the vendor has
        no lookup endpoint, persist the mapping yourself.
        """
        return None

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------
    async def get_user(self, user_ref: str) -> RemoteUser:
        raise NotImplementedError

    async def renew_user(
        self, user_ref: str, *, duration_seconds: int | None, idempotency_key: str | None = None
    ) -> RemoteUser:
        raise NotImplementedError

    async def set_limits(
        self,
        user_ref: str,
        *,
        traffic_limit_bytes: int | None = None,
        device_limit: int | None = None,
        speed_limit_down_kbps: int | None = None,
        speed_limit_up_kbps: int | None = None,
    ) -> RemoteUser:
        """``None`` means "leave this field unchanged", not "clear it"."""
        raise NotImplementedError

    async def reset_traffic(self, user_ref: str) -> RemoteUser:
        raise NotImplementedError

    async def set_enabled(self, user_ref: str, enabled: bool) -> RemoteUser:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Devices
    # ------------------------------------------------------------------
    async def list_devices(self, user_ref: str) -> list[RemoteDevice]:
        raise NotImplementedError

    async def create_device(self, user_ref: str, *, name: str) -> RemoteDevice:
        raise NotImplementedError

    async def delete_device(self, device_ref: str) -> None:
        raise NotImplementedError

    async def regenerate_device(self, device_ref: str) -> RemoteDevice:
        """Issue new keys.  Not idempotent — do not retry inside the adapter."""
        raise NotImplementedError

    async def device_config(self, device_ref: str) -> str:
        """The client configuration text.  It contains a private key: never log it."""
        raise NotImplementedError

    async def device_qr(self, device_ref: str) -> bytes | None:
        """Optional.  ``None`` is a perfectly good answer — the app renders the
        QR locally from :meth:`device_config` when the node cannot.
        """
        return None

    # ------------------------------------------------------------------
    # Subscription
    # ------------------------------------------------------------------
    async def subscription_link(self, user_ref: str) -> SubscriptionLink:
        """The private subscription path (a bearer capability — treat as a secret)."""
        raise NotImplementedError

    async def rotate_subscription(self, user_ref: str) -> SubscriptionLink:
        """Optional; drop :data:`CAP_SUBSCRIPTION_ROTATE` if unsupported."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Optional: automatic renewal
    # ------------------------------------------------------------------
    async def queue_next_plan(self, user_ref: str, plan_ref: str, *, carry_unused_traffic: bool = False) -> bool:
        """Optional; requires :data:`CAP_NEXT_PLAN`."""
        raise NotImplementedError

    async def clear_next_plan(self, user_ref: str) -> bool:
        """Optional; requires :data:`CAP_NEXT_PLAN`."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Optional: a small helper that shows how to translate vendor errors
    # ------------------------------------------------------------------
    @staticmethod
    def _raise_from_response(status: int, payload: Any) -> None:
        """Example of mapping a vendor error onto our error types.

        The domain relies on :class:`app.core.errors.PanelError` subclasses to
        produce a Persian message and to decide whether a retry is safe.
        """
        raise PanelError(f"panel returned {status}: {str(payload)[:200]}")


__all__ = ["ExampleProvider"]
