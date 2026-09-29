"""Panel layer: the port, the registry and the built-in adapters.

Public API for the rest of the application::

    from app.panels import PanelProvider, panel_manager, registry
    provider = await panel_manager.provider_for(panel)
    await provider.purchase(...)

Nothing outside this package should import a vendor client such as
:class:`app.panels.client.WGGuardClient` — that is an implementation detail of
the ``wg_guard`` adapter.
"""

from __future__ import annotations

# Importing the adapters is what registers them.
import app.panels.providers  # noqa: F401  (side-effect import, must follow the names above)
from app.panels import registry
from app.panels.base import (
    CAP_ATOMIC_PURCHASE,
    CAP_NEXT_PLAN,
    CAP_PLAN_SYNC,
    CAP_PURCHASE_RECOVERY,
    CAP_SUBSCRIPTION_ROTATE,
    CAP_WEBHOOKS,
    PanelProvider,
    ProviderError,
    UnsupportedCapability,
)
from app.panels.manager import PanelManager, PanelSnapshot, panel_manager
from app.panels.models import (
    NodeInfo,
    PlanSpec,
    ProviderHealth,
    PurchaseResult,
    RemoteDevice,
    RemoteInterface,
    RemoteStatus,
    RemoteUser,
    SubscriptionLink,
)

__all__ = [
    "CAP_ATOMIC_PURCHASE",
    "CAP_NEXT_PLAN",
    "CAP_PLAN_SYNC",
    "CAP_PURCHASE_RECOVERY",
    "CAP_SUBSCRIPTION_ROTATE",
    "CAP_WEBHOOKS",
    "NodeInfo",
    "PanelManager",
    "PanelProvider",
    "PanelSnapshot",
    "PlanSpec",
    "ProviderError",
    "ProviderHealth",
    "PurchaseResult",
    "RemoteDevice",
    "RemoteInterface",
    "RemoteStatus",
    "RemoteUser",
    "SubscriptionLink",
    "UnsupportedCapability",
    "panel_manager",
    "registry",
]
