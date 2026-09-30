"""October API contract: UTF-8 names, patch exceptions and atomic devices."""

import httpx
import pytest
from pydantic import ValidationError

from app.core.errors import PanelConflict, PanelValidation
from app.panels.models import PlanSpec
from app.panels.providers.wgguard import WGGuardProvider
from app.panels.schemas import OperationResult, PlanPatch, UserCreate, UserPatch


async def test_template_names_respect_utf8_byte_limit(panel_transport, wg_client):
    provider = WGGuardProvider(
        name="test", base_url="http://panel.test", token="wg_test_token", transport=panel_transport
    )
    try:
        ref = await provider.ensure_plan(PlanSpec(name="  " + "بسته🛰️" * 30 + "  "))
        name = (await wg_client.get_plan(ref)).name
        assert name and len(name.encode("utf-8")) <= 64
        assert "\ufffd" not in name and name == name.strip()
    finally:
        await provider.aclose()


async def test_template_duration_null_is_noop_but_fresh_template_can_be_unlimited(wg_client, panel_transport):
    first = await wg_client.create_plan(PlanPatch(name="original", duration_seconds=2592000))
    patched = await wg_client.update_plan(first.id, PlanPatch(duration_seconds=None))
    assert patched.duration_seconds == 2592000
    provider = WGGuardProvider(
        name="test", base_url="http://panel.test", token="wg_test_token", transport=panel_transport
    )
    try:
        second = await provider.ensure_plan(PlanSpec(name="unlimited"), existing_ref=first.id)
        assert second != first.id
        assert (await wg_client.get_plan(second)).duration_seconds is None
        assert (await wg_client.get_plan(first.id)).duration_seconds == 2592000
    finally:
        await provider.aclose()


async def test_template_update_is_verified_when_node_ignores_a_value():
    requests = []

    def respond(request):
        requests.append(request.method)
        return httpx.Response(
            200,
            json={
                "id": "t",
                "name": "x",
                "duration_seconds": 86400,
                "start_policy": "first_connection",
                "device_limit": 1,
            },
        )

    provider = WGGuardProvider(
        name="test", base_url="https://node.test", token="test-only", transport=httpx.MockTransport(respond)
    )
    try:
        with pytest.raises(PanelValidation):
            await provider.ensure_plan(PlanSpec(name="x", duration_seconds=2592000), existing_ref="t")
        assert requests == ["GET", "PATCH", "GET"]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("model", [UserCreate, UserPatch, PlanPatch])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device_limit", 0),
        ("speed_limit_up_kbps", 0),
        ("speed_limit_down_kbps", -1),
        ("traffic_limit_bytes", -1),
        ("duration_seconds", 0),
    ],
)
def test_request_constraints_follow_new_contract(model, field, value):
    values = {field: value}
    if model is UserCreate:
        values["username"] = "validuser"
    with pytest.raises(ValidationError):
        model(**values)


def test_zero_quota_and_unlimited_limits_remain_distinct():
    assert UserPatch(traffic_limit_bytes=0).model_dump(exclude_unset=True) == {"traffic_limit_bytes": 0}
    assert UserPatch(speed_limit_up_kbps=None).model_dump(exclude_unset=True) == {"speed_limit_up_kbps": None}


@pytest.mark.parametrize("ids", [[], ["wrong"], ["d1", "d1"], ["d1"] * 101])
def test_inconsistent_device_result_cannot_be_accepted(ids):
    with pytest.raises(ValidationError):
        OperationResult(operation_id="op", user_id="u", device_id="d1", device_ids=ids)


def test_legacy_operation_result_keeps_single_device_identity():
    assert OperationResult(operation_id="op", user_id="u", device_id="d1").all_device_ids == ("d1",)


async def test_default_purchase_allocates_one_device_not_the_cap(wg_client):
    template = await wg_client.create_plan(PlanPatch(name="cap", device_limit=3))
    result = await wg_client.create_purchase(template.id, idempotency_key="single", username="single")
    assert len(result.all_device_ids) == 1
    assert (await wg_client.get_user(result.user_id)).device_limit == 3


async def test_atomic_multi_device_purchase_and_recovery_preserve_all_ids(wg_client, panel_transport):
    template = await wg_client.create_plan(PlanPatch(name="multi", device_limit=3))
    provider = WGGuardProvider(
        name="test", base_url="http://panel.test", token="wg_test_token", transport=panel_transport
    )
    try:
        result = await provider.purchase(
            plan_ref=template.id, username="multi", device_name="phone", device_count=3, idempotency_key="multi-order"
        )
        replay = await provider.purchase(
            plan_ref=template.id, username="multi", device_name="phone", device_count=3, idempotency_key="multi-order"
        )
        recovered = await provider.recover_purchase("multi-order")
        assert len(result.all_device_ids) == 3
        assert result.all_device_ids == replay.all_device_ids == recovered.all_device_ids
        devices = await provider.list_devices(result.user_id)
        assert {item.name for item in devices} == {"phone-1", "phone-2", "phone-3"}
        assert len({item.public_key for item in devices}) == 3
        with pytest.raises(PanelConflict):
            await provider.purchase(
                plan_ref=template.id,
                username="multi",
                device_name="phone",
                device_count=2,
                idempotency_key="multi-order",
            )
    finally:
        await provider.aclose()


async def test_multi_device_failure_rolls_back_user_devices_and_result(wg_client, mock_panel, monkeypatch):
    import mock_wg_panel.app as module

    from app.core.errors import PanelNotFound, PanelUnavailable

    template = await wg_client.create_plan(PlanPatch(name="rollback", device_limit=3))
    original = module._new_device
    calls = []

    def fail_second(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise module.ApiError(503, "NODE_UNAVAILABLE", "fixture failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "_new_device", fail_second)
    with pytest.raises(PanelUnavailable):
        await wg_client.create_purchase(
            template.id, username="rollback", device_count=3, idempotency_key="rollback-key"
        )
    assert mock_panel.state.store.users == {} and mock_panel.state.store.devices == {}
    assert mock_panel.state.store.device_secrets == {}
    with pytest.raises(PanelNotFound):
        await wg_client.operation_result("rollback-key")


async def test_user_patch_duration_and_metadata_do_not_change_expiry_or_annotations(wg_client):
    user = await wg_client.create_user(
        {"username": "patchrules", "duration_seconds": 86400, "metadata": {"label": "saved"}}
    )
    changed = await wg_client.update_user(user.id, UserPatch(duration_seconds=2592000, metadata={"label": "ignored"}))
    assert changed.expires_at == user.expires_at
    assert changed.duration_seconds == 2592000
    assert changed.model_dump()["metadata"] == {"label": "saved"}
    unchanged = await wg_client.update_user(user.id, UserPatch(duration_seconds=None))
    assert unchanged.duration_seconds == 2592000


async def test_user_creation_with_template_uses_saved_terms(wg_client):
    template = await wg_client.create_plan(
        PlanPatch(name="terms", duration_seconds=86400, traffic_limit_bytes=1000000000, device_limit=3)
    )
    user = await wg_client.create_user(
        {
            "username": "fromtemplate",
            "template_id": template.id,
            "traffic_limit_bytes": 5,
            "duration_seconds": 60,
            "device_limit": 1,
        }
    )
    assert user.traffic_limit_bytes == 1000000000 and user.duration_seconds == 86400 and user.device_limit == 3


async def test_stats_use_charged_bytes_and_optional_observations(wg_client):
    user = await wg_client.create_user({"username": "stats", "traffic_limit_bytes": 100})
    await wg_client.add_traffic(user.id, rx_bytes=100, tx_bytes=20)
    stats = await wg_client.user_stats(user.id)
    assert stats["traffic_used_total"] == 120 and stats["traffic_percent_used"] == 120
    assert stats["traffic_remaining_bytes"] == 0
    total = await wg_client.stats()
    assert total["traffic_used_rx"] == 100 and total["traffic_used_tx"] == 20
    assert isinstance(total["devices"], int)


async def test_stats_omit_unavailable_percent_and_handshake_observations(wg_client):
    user = await wg_client.create_user({"username": "nostats"})
    device = await wg_client.create_device(user.id, name="phone")
    stats = await wg_client.user_stats(user.id)
    assert stats["traffic_limit_bytes"] is None
    assert "traffic_percent_used" not in stats and "traffic_remaining_bytes" not in stats
    peer = await wg_client.device_stats(device.id)
    assert peer["last_handshake_at"] is None
    assert "online" not in peer and "online_window_seconds" not in peer
