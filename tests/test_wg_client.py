"""WG-Guard client tests against the in-repo mock node.

No network: every request is routed through ``httpx.ASGITransport``.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.errors import PanelAuthError, PanelConflict, PanelError, PanelNotFound, PanelValidation
from app.panels.client import WGGuardClient
from app.panels.schemas import PlanPatch


async def test_health_is_public(wg_client: WGGuardClient) -> None:
    status = await wg_client.health()
    assert status.status


async def test_missing_token_is_rejected(panel_transport: httpx.ASGITransport) -> None:
    client = WGGuardClient("http://panel.test", "wrong-token", transport=panel_transport, max_retries=0)
    async with client:
        with pytest.raises(PanelAuthError):
            await client.node()


async def test_node_and_telemetry(wg_client: WGGuardClient) -> None:
    node = await wg_client.node()
    assert node.node_id is not None
    telemetry = await wg_client.telemetry(points=5)
    assert telemetry.cadence_seconds >= 1
    assert len(telemetry.points) <= 5


async def test_plan_lifecycle(wg_client: WGGuardClient) -> None:
    created = await wg_client.create_plan(
        PlanPatch(name="تست پلن", traffic_limit_bytes=1024**3, duration_seconds=86400, device_limit=2)
    )
    assert created.id
    fetched = await wg_client.get_plan(created.id)
    assert fetched.name == "تست پلن"
    await wg_client.update_plan(created.id, PlanPatch(name="تست پلن", device_limit=3))
    assert (await wg_client.get_plan(created.id)).device_limit == 3
    await wg_client.delete_plan(created.id)
    with pytest.raises(PanelNotFound):
        await wg_client.get_plan(created.id)


async def test_purchase_is_idempotent(wg_client: WGGuardClient) -> None:
    plan = await wg_client.create_plan(
        PlanPatch(name="پلن خرید", traffic_limit_bytes=10 * 1024**3, duration_seconds=30 * 86400)
    )
    key = "test-idem-key-0001"

    first = await wg_client.create_purchase(plan.id, idempotency_key=key, username="alice01")
    second = await wg_client.create_purchase(plan.id, idempotency_key=key, username="alice01")

    assert first.user_id == second.user_id
    assert first.device_id == second.device_id
    assert first.operation_id == second.operation_id

    # The customer link and config must be retrievable afterwards.
    link = await wg_client.customer_subscription(first.user_id)
    assert link.path.startswith("/sub/")
    config = await wg_client.device_config(first.device_id)
    assert "[Interface]" in config
    assert "PrivateKey" in config
    assert "[Peer]" in config


async def test_purchase_with_same_key_different_body_conflicts(wg_client: WGGuardClient) -> None:
    plan_a = await wg_client.create_plan(PlanPatch(name="A", duration_seconds=86400))
    plan_b = await wg_client.create_plan(PlanPatch(name="B", duration_seconds=86400))
    key = "test-idem-key-0002"

    await wg_client.create_purchase(plan_a.id, idempotency_key=key, username="bob01")
    with pytest.raises(PanelConflict):
        await wg_client.create_purchase(plan_b.id, idempotency_key=key, username="bob01")


async def test_operation_result_lookup(wg_client: WGGuardClient) -> None:
    plan = await wg_client.create_plan(PlanPatch(name="C", duration_seconds=86400))
    key = "test-idem-key-0003"
    created = await wg_client.create_purchase(plan.id, idempotency_key=key, username="carol1")

    found = await wg_client.operation_result(key)
    assert found.user_id == created.user_id

    with pytest.raises(PanelNotFound):
        await wg_client.operation_result("never-used-key")


async def test_users_pagination_and_filters(wg_client: WGGuardClient) -> None:
    plan = await wg_client.create_plan(PlanPatch(name="P", duration_seconds=86400))
    for index in range(5):
        await wg_client.create_user({"username": f"user{index:02d}", "plan_id": plan.id, "duration_seconds": 86400})

    page = await wg_client.list_users(limit=2)
    assert len(page.items) == 2
    assert page.next_cursor

    seen: list[str] = []
    async for user in wg_client.iter_users(page_size=2):
        seen.append(user.username)
    assert len(seen) == 5
    assert len(set(seen)) == 5


async def test_user_traffic_and_renew(wg_client: WGGuardClient) -> None:
    plan = await wg_client.create_plan(PlanPatch(name="T", traffic_limit_bytes=1024**3, duration_seconds=86400))
    result = await wg_client.create_purchase(plan.id, idempotency_key="traffic-key-1", username="dave01")

    user = await wg_client.add_traffic(result.user_id, rx_bytes=100, tx_bytes=200)
    assert user.traffic_used_total >= 300

    reset = await wg_client.reset_traffic(result.user_id)
    assert reset.traffic_used_total == 0

    renewed = await wg_client.renew_user(result.user_id, duration_seconds=86400)
    assert renewed.id == result.user_id


async def test_devices_and_subscription_rotation(wg_client: WGGuardClient) -> None:
    plan = await wg_client.create_plan(PlanPatch(name="D", duration_seconds=86400, device_limit=3))
    result = await wg_client.create_purchase(plan.id, idempotency_key="dev-key-1", username="erin01")

    devices = await wg_client.list_devices(result.user_id)
    assert len(devices) == 1

    extra = await wg_client.create_device(result.user_id, name="laptop")
    assert extra.name == "laptop"
    assert len(await wg_client.list_devices(result.user_id)) == 2

    before = await wg_client.customer_subscription(result.user_id)
    rotation = await wg_client.rotate_customer_access(result.user_id)
    assert rotation.devices_rotated >= 1
    assert rotation.path != before.path

    await wg_client.delete_device(extra.id)
    assert len(await wg_client.list_devices(result.user_id)) == 1


async def test_error_envelope_is_parsed(wg_client: WGGuardClient) -> None:
    with pytest.raises(PanelError):
        await wg_client.get_user("does-not-exist")


async def test_missing_idempotency_key_is_rejected(wg_client: WGGuardClient) -> None:
    """An empty key means "no header", which the node refuses — by design."""
    plan = await wg_client.create_plan(PlanPatch(name="E", duration_seconds=86400))
    with pytest.raises(PanelValidation):
        await wg_client.create_purchase(plan.id, idempotency_key="")


async def test_retry_on_transient_failure(panel_transport: httpx.ASGITransport, panel_client) -> None:
    """A 503 on a GET is retried and eventually succeeds."""
    await panel_client.post("/__mock__/fail", json={"path": "/api/v1/node", "count": 1, "status": 503})

    client = WGGuardClient(
        "http://panel.test",
        "wg_test_token",
        transport=panel_transport,
        max_retries=2,
        backoff_base=0.01,
    )
    async with client:
        node = await client.node()
    assert node is not None
