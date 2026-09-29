"""The panel-provider port.

This is the single interface the rest of the application programs against.
WG-Guard implements it today (``app.panels.providers.wgguard``); a PasarGuard,
Marzban or x-ui adapter implements the same methods and is registered in
``app/panels/registry.py``.  **No business logic may import a vendor client.**

Design notes
------------
* The interface is expressed in the canonical models from
  :mod:`app.panels.models`, never in vendor payloads.
* Everything is ``async`` and cancellation-safe: a provider must not swallow
  ``asyncio.CancelledError``.
* Failures are raised as :mod:`app.core.errors` types, so callers do not need
  to know which HTTP client produced them.
* Optional features are advertised through ``capabilities`` and guarded by
  :meth:`PanelProvider.supports`, so a new backend can be partial without the
  domain having to special-case it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from app.panels.models import (
    NodeInfo,
    PlanSpec,
    ProviderHealth,
    PurchaseResult,
    RemoteDevice,
    RemoteUser,
    SubscriptionLink,
)

# ---------------------------------------------------------------------------
# Capability flags
# ---------------------------------------------------------------------------
#: A node-side plan can be created/updated by us.  When absent, the operator
#: must create the plan in the vendor panel and paste its id onto our Plan row.
CAP_PLAN_SYNC = "plan_sync"

#: Purchases are atomic (user + first device + subscription in one call) and
#: idempotent under a client-supplied key.  When absent, provisioning composes
#: the steps itself and the caller must tolerate partial failure.
CAP_ATOMIC_PURCHASE = "atomic_purchase"

#: A committed purchase can be looked up again by its idempotency key.
CAP_PURCHASE_RECOVERY = "purchase_recovery"

#: The node can render a QR code.  When absent we generate it locally (we
#: always can, so this only avoids a round trip).
CAP_NODE_QR = "node_qr"

#: A successor plan can be queued for automatic renewal on the node.
CAP_NEXT_PLAN = "next_plan"

#: The customer link can be rotated (new keys for every device at once).
CAP_SUBSCRIPTION_ROTATE = "subscription_rotate"

#: Traffic can be set to an absolute value / topped up.
CAP_TRAFFIC_WRITE = "traffic_write"

#: The node can push signed lifecycle events to us.
CAP_WEBHOOKS = "webhooks"

#: Per-user traffic time series.
CAP_TRAFFIC_SERIES = "traffic_series"

#: Multiple tunnel interfaces/profiles exist and can be chosen per plan.
CAP_INTERFACES = "interfaces"


class ProviderError(Exception):
    """Base class for adapter-level failures (mapped to ``AppError`` by callers)."""


class UnsupportedCapability(ProviderError):
    """Raised when a caller uses a feature the backend does not advertise."""

    def __init__(self, capability: str, provider: str) -> None:
        self.capability = capability
        self.provider = provider
        super().__init__(f"{provider} does not support '{capability}'")


class PanelProvider(ABC):
    """Port implemented by every VPN backend adapter."""

    #: Stable machine identifier stored on ``Panel.kind``.
    kind: ClassVar[str] = ""
    #: Human label shown in the admin panel (Persian).
    label: ClassVar[str] = ""
    #: One-line description of what the adapter talks to.
    description: ClassVar[str] = ""
    #: Feature flags, see the ``CAP_*`` constants above.
    capabilities: ClassVar[frozenset[str]] = frozenset()

    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        token: str,
        subscription_base_url: str | None = None,
        options: dict | None = None,
        transport: object | None = None,
        timeout: float = 30.0,
        connect_timeout: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.subscription_base_url = (subscription_base_url or "").rstrip("/") or None
        self.options = options or {}
        self.transport = transport
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.max_retries = max_retries

    # -- helpers -----------------------------------------------------------
    @classmethod
    def supports(cls, capability: str) -> bool:
        return capability in cls.capabilities

    def require(self, capability: str) -> None:
        """Raise :class:`UnsupportedCapability` unless the backend has it."""
        if not self.supports(capability):
            raise UnsupportedCapability(capability, self.kind or type(self).__name__)

    @property
    def public_origin(self) -> str:
        """Origin used to build customer-facing subscription URLs."""
        return self.subscription_base_url or self.base_url

    # -- lifecycle ---------------------------------------------------------
    async def aclose(self) -> None:  # noqa: B027 - optional override
        """Release held connections.  Safe to call more than once."""

    # -- discovery ---------------------------------------------------------
    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Cheap liveness/capability probe used by the health job and the UI."""

    @abstractmethod
    async def node_info(self) -> NodeInfo:
        """Identity, version and interface inventory."""

    # -- catalogue ---------------------------------------------------------
    @abstractmethod
    async def ensure_plan(self, spec: PlanSpec, *, existing_ref: str | None = None) -> str:
        """Return a valid node-side plan reference, creating/updating as needed.

        ``existing_ref`` is what we stored last time; an adapter may verify it,
        recreate it, or ignore it and return the same value.
        """

    # -- provisioning ------------------------------------------------------
    @abstractmethod
    async def purchase(
        self,
        *,
        plan_ref: str,
        username: str,
        device_name: str,
        idempotency_key: str,
    ) -> PurchaseResult:
        """Provision a customer + first device + subscription link.

        The adapter must be **exactly-once** for a given ``idempotency_key``:
        retrying with the same key returns the original result rather than
        creating a second account.
        """

    async def recover_purchase(self, idempotency_key: str) -> PurchaseResult | None:
        """Look up a purchase that may have committed before a failure.

        Adapters without :data:`CAP_PURCHASE_RECOVERY` return ``None``, which
        tells the caller "not committed, safe to retry".
        """
        return None

    # -- users -------------------------------------------------------------
    @abstractmethod
    async def get_user(self, user_ref: str) -> RemoteUser:
        """Fetch one account."""

    @abstractmethod
    async def renew_user(
        self, user_ref: str, *, duration_seconds: int | None, idempotency_key: str | None = None
    ) -> RemoteUser:
        """Extend an account and return its new state."""

    @abstractmethod
    async def set_limits(
        self,
        user_ref: str,
        *,
        traffic_limit_bytes: int | None = None,
        device_limit: int | None = None,
        speed_limit_down_kbps: int | None = None,
        speed_limit_up_kbps: int | None = None,
    ) -> RemoteUser:
        """Update quota/limit fields.  ``None`` means "leave unchanged"."""

    @abstractmethod
    async def reset_traffic(self, user_ref: str) -> RemoteUser:
        """Zero the usage counters."""

    @abstractmethod
    async def set_enabled(self, user_ref: str, enabled: bool) -> RemoteUser:
        """Enable or disable an account."""

    # -- devices -----------------------------------------------------------
    @abstractmethod
    async def list_devices(self, user_ref: str) -> list[RemoteDevice]:
        """Every peer belonging to an account."""

    @abstractmethod
    async def create_device(self, user_ref: str, *, name: str) -> RemoteDevice:
        """Add a peer (keys generated by the node)."""

    @abstractmethod
    async def delete_device(self, device_ref: str) -> None:
        """Remove a peer and release its address."""

    @abstractmethod
    async def regenerate_device(self, device_ref: str) -> RemoteDevice:
        """Issue new keys for a peer.

        Must **not** be retried automatically: the vendor contract offers no
        idempotency key for it.
        """

    @abstractmethod
    async def device_config(self, device_ref: str) -> str:
        """The canonical client configuration text (contains a private key)."""

    async def device_qr(self, device_ref: str) -> bytes | None:
        """A PNG QR code, or ``None`` when the adapter cannot render one.

        Returning ``None`` is normal: the application generates the QR locally
        from :meth:`device_config`.
        """
        return None

    # -- subscription ------------------------------------------------------
    @abstractmethod
    async def subscription_link(self, user_ref: str) -> SubscriptionLink:
        """The customer's private subscription path (a bearer capability)."""

    async def rotate_subscription(self, user_ref: str) -> SubscriptionLink:
        """Invalidate the old link/configs and issue a new one."""
        self.require(CAP_SUBSCRIPTION_ROTATE)
        raise UnsupportedCapability(CAP_SUBSCRIPTION_ROTATE, self.kind)

    # -- optional: automatic renewal ---------------------------------------
    async def queue_next_plan(self, user_ref: str, plan_ref: str, *, carry_unused_traffic: bool = False) -> bool:
        """Queue a successor plan for automatic activation."""
        self.require(CAP_NEXT_PLAN)
        return False

    async def clear_next_plan(self, user_ref: str) -> bool:
        """Cancel a queued successor plan."""
        self.require(CAP_NEXT_PLAN)
        return False


__all__ = [
    "CAP_ATOMIC_PURCHASE",
    "CAP_INTERFACES",
    "CAP_NEXT_PLAN",
    "CAP_NODE_QR",
    "CAP_PLAN_SYNC",
    "CAP_PURCHASE_RECOVERY",
    "CAP_SUBSCRIPTION_ROTATE",
    "CAP_TRAFFIC_SERIES",
    "CAP_TRAFFIC_WRITE",
    "CAP_WEBHOOKS",
    "PanelProvider",
    "ProviderError",
    "UnsupportedCapability",
]
