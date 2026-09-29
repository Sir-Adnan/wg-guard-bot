"""The panel-provider abstraction.

These tests pin the contract that makes new backends (PasarGuard, Marzban, …)
addable without touching business logic:

* the registry exposes what the admin panel renders;
* ``Panel.kind`` selects the adapter;
* the WG-Guard adapter maps vendor payloads onto the canonical models;
* a provider that does not implement a capability says so instead of pretending.
"""

from __future__ import annotations

import pytest

from app.core.errors import PanelError
from app.panels import registry
from app.panels.base import (
    CAP_ATOMIC_PURCHASE,
    CAP_NEXT_PLAN,
    CAP_PLAN_SYNC,
    PanelProvider,
    UnsupportedCapability,
)
from app.panels.manager import panel_manager
from app.panels.models import PlanSpec, PurchaseResult, RemoteUser

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_wg_guard_is_registered() -> None:
    assert registry.has("wg_guard")
    provider = registry.get("wg_guard")
    assert provider.kind == "wg_guard"
    assert provider.label
    assert CAP_ATOMIC_PURCHASE in provider.capabilities
    assert CAP_PLAN_SYNC in provider.capabilities


def test_unknown_kind_falls_back_to_default() -> None:
    assert registry.get(None) is registry.get(registry.DEFAULT_KIND)
    assert registry.get("") is registry.get(registry.DEFAULT_KIND)
    with pytest.raises(LookupError):
        registry.get("does-not-exist")


def test_choices_are_ready_for_a_select_box() -> None:
    choices = registry.choices()
    assert choices
    for kind, label, description in choices:
        assert kind and label and isinstance(description, str)


def test_describe_lists_capabilities() -> None:
    described = registry.describe("wg_guard")
    assert described["kind"] == "wg_guard"
    assert "atomic_purchase" in described["capabilities"]


def test_every_provider_declares_the_required_metadata() -> None:
    for provider in registry.all_providers():
        assert provider.kind, provider.__name__
        assert provider.label, provider.__name__
        assert isinstance(provider.capabilities, frozenset)


def test_example_provider_documents_but_does_not_lie() -> None:
    """The template adapter must refuse work rather than silently succeed."""
    example = registry.get("example")
    assert example is not PanelProvider

    instance = example(name="t", base_url="http://x", token="k")
    with pytest.raises(NotImplementedError):
        import asyncio

        asyncio.get_event_loop_policy()
        asyncio.run(instance.node_info())


def test_unsupported_capability_is_explicit() -> None:
    example = registry.get("example")
    instance = example(name="t", base_url="http://x", token="k")
    with pytest.raises(UnsupportedCapability) as excinfo:
        instance.require(CAP_NEXT_PLAN)
    assert excinfo.value.capability == CAP_NEXT_PLAN
    # A capability the adapter does declare must not raise.
    instance.require(CAP_ATOMIC_PURCHASE)


# ---------------------------------------------------------------------------
# Resolution through the manager
# ---------------------------------------------------------------------------


async def test_manager_builds_the_configured_adapter(session, panel_row) -> None:
    panel_row.kind = "wg_guard"
    await session.flush()

    provider = await panel_manager.provider_for(panel_row)
    assert provider.kind == "wg_guard"
    assert isinstance(provider, PanelProvider)
    # Cached: the same instance is returned for the same panel.
    assert await panel_manager.provider_for(panel_row) is provider


async def test_manager_rejects_an_unknown_kind(session, panel_row) -> None:
    panel_row.kind = "not-a-real-panel"
    await session.flush()
    panel_manager.forget_all()

    with pytest.raises(PanelError) as excinfo:
        await panel_manager.provider_for(panel_row)
    assert "پشتیبانی" in excinfo.value.message or "نوع پنل" in excinfo.value.message


async def test_health_probe_uses_the_adapter(session, panel_row) -> None:
    health = await panel_manager.check_health(session, panel_row, force=True)
    assert health.value in ("online", "degraded")
    assert panel_row.node_version


async def test_snapshots_carry_the_provider_kind(session, panel_row) -> None:
    snapshots = await panel_manager.list_snapshots(session)
    assert snapshots
    assert snapshots[0].kind == "wg_guard"
    assert snapshots[0].kind_label == "WG-Guard"


# ---------------------------------------------------------------------------
# Canonical mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("vendor", "expected"),
    [
        ("active", "active"),
        ("waiting_first_connection", "waiting_first_connection"),
        ("disabled", "disabled"),
        ("suspended", "disabled"),  # vendor-only state folded into ours
        ("expired", "expired"),
        ("traffic_exceeded", "traffic_exceeded"),
        ("something-new", "active"),  # additive vendor change must not crash
    ],
)
def test_status_normalisation(vendor: str, expected: str) -> None:
    from app.panels.providers.wgguard import normalise_status

    assert normalise_status(vendor) == expected


async def test_purchase_and_recovery_return_canonical_models(session, panel_row) -> None:
    provider = await panel_manager.provider_for(panel_row)

    plan_ref = await provider.ensure_plan(
        PlanSpec(
            name="پلن تستی",
            traffic_limit_bytes=5 * 1024**3,
            duration_seconds=30 * 86400,
            device_limit=2,
        )
    )
    assert plan_ref

    key = "provider-test-key-1"
    result = await provider.purchase(
        plan_ref=plan_ref, username="provideruser", device_name="device-1", idempotency_key=key
    )
    assert isinstance(result, PurchaseResult)
    assert result.user_id and result.device_id
    assert result.username == "provideruser"

    # Idempotent replay returns the same account, not a second one.
    again = await provider.purchase(
        plan_ref=plan_ref, username="provideruser", device_name="device-1", idempotency_key=key
    )
    assert again.user_id == result.user_id

    recovered = await provider.recover_purchase(key)
    assert recovered is not None
    assert recovered.user_id == result.user_id
    assert recovered.recovered is True

    assert await provider.recover_purchase("never-used") is None


async def test_user_and_device_round_trip(session, panel_row) -> None:
    provider = await panel_manager.provider_for(panel_row)
    plan_ref = await provider.ensure_plan(PlanSpec(name="P", duration_seconds=86400, device_limit=3))
    created = await provider.purchase(
        plan_ref=plan_ref, username="roundtrip", device_name="device-1", idempotency_key="rt-key-1"
    )

    user = await provider.get_user(created.user_id)
    assert isinstance(user, RemoteUser)
    assert user.status in ("active", "waiting_first_connection")
    assert user.enabled is True

    devices = await provider.list_devices(created.user_id)
    assert len(devices) == 1
    assert devices[0].id == created.device_id

    config = await provider.device_config(created.device_id)
    assert "[Interface]" in config and "PrivateKey" in config

    link = await provider.subscription_link(created.user_id)
    assert link.path.startswith("/sub/")

    rotated = await provider.rotate_subscription(created.user_id)
    assert rotated.path != link.path
    assert rotated.devices_rotated >= 1


async def test_set_limits_and_reset(session, panel_row) -> None:
    provider = await panel_manager.provider_for(panel_row)
    plan_ref = await provider.ensure_plan(PlanSpec(name="L", duration_seconds=86400))
    created = await provider.purchase(
        plan_ref=plan_ref, username="limits", device_name="device-1", idempotency_key="lim-key-1"
    )

    updated = await provider.set_limits(created.user_id, traffic_limit_bytes=7 * 1024**3)
    assert updated.traffic_limit_bytes == 7 * 1024**3

    reset = await provider.reset_traffic(created.user_id)
    assert reset.traffic_used_bytes == 0

    disabled = await provider.set_enabled(created.user_id, False)
    assert disabled.enabled is False
    assert disabled.status == "disabled"

    enabled = await provider.set_enabled(created.user_id, True)
    assert enabled.enabled is True
