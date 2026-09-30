"""Independent checks against the official snapshot, beyond the mock's opinion."""

import json
from pathlib import Path

import httpx
import pytest

from app.core.errors import PanelError, PanelUnavailable
from app.core.money import kbps_to_mbps
from app.panels.client import WGGuardClient
from app.panels.models import PlanSpec
from app.panels.providers.wgguard import WGGuardProvider
from app.panels.schemas import OperationResult, PlanPatch, UserPatch

SPEC = json.loads(
    (Path(__file__).resolve().parents[1] / "docs/upstream-api/wg-guard-openapi.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    ("kbps", "expected"), [(100000, "100"), (20000, "20"), (512, "0.512"), (1500, "1.5"), (0, "0")]
)
def test_speed_is_decimal_bits_not_binary_bytes(kbps, expected):
    assert kbps_to_mbps(kbps) == expected


def test_rial_percentage_is_exact_above_float_integer_precision():
    from decimal import ROUND_HALF_EVEN

    from app.core.money import percent_of_rial

    assert percent_of_rial(9007199254740999, 100) == 9007199254740999
    assert percent_of_rial(5, 10, rounding=ROUND_HALF_EVEN) == 0
    assert percent_of_rial(15, 10, rounding=ROUND_HALF_EVEN) == 2


async def test_unsupported_recovery_does_not_claim_a_purchase_is_absent():
    from app.panels.base import PanelProvider, UnsupportedCapability

    provider = WGGuardProvider(name="test", base_url="https://node.test", token="test-only")
    try:
        with pytest.raises(UnsupportedCapability):
            await PanelProvider.recover_purchase(provider, "order-key")
    finally:
        await provider.aclose()


@pytest.mark.db
async def test_first_text_read_and_adapter_errors_use_operator_overrides(session):
    from app.core.locales import default_text
    from app.services.texts import TextStore

    store = TextStore()
    try:
        await store.set_text(session, "error.node_unavailable", "operator-reviewed copy")
        store.invalidate()
        assert await store.get("error.node_unavailable", session) == "operator-reviewed copy"
        assert default_text("error.node_unavailable") == "operator-reviewed copy"
        await store.reset_text(session, "error.node_unavailable")
        assert default_text("error.node_unavailable") != "operator-reviewed copy"
    finally:
        store.invalidate()


async def test_purchase_uses_official_template_field_and_optional_result_ref():
    def respond(request):
        assert request.url.path in SPEC["paths"]
        body = json.loads(request.content)
        schema = SPEC["components"]["schemas"]["PurchaseRequest"]
        assert set(body) <= set(schema["properties"])
        assert body == {"template_id": "t1", "username": "buyer", "device_name": "phone"}
        assert request.headers["Idempotency-Key"] == "order-key"
        # Direct owner purchases legitimately have no template_id in their result.
        return httpx.Response(
            201,
            json={"operation_id": "op1", "kind": "purchase", "state": "committed", "user_id": "u1", "device_id": "d1"},
        )

    async with WGGuardClient("https://node.test", "test-only", transport=httpx.MockTransport(respond)) as client:
        result = await client.create_purchase("t1", username="buyer", device_name="phone", idempotency_key="order-key")
    assert result.user_id == "u1" and result.template_id is None
    assert OperationResult.model_validate(result.model_dump()).device_id == "d1"


async def test_typed_patch_distinguishes_omission_null_and_value(wg_client):
    user = await wg_client.create_user(
        {"username": "tristate", "speed_limit_up_kbps": 20000, "speed_limit_down_kbps": 100000}
    )
    changed = await wg_client.update_user(user.id, UserPatch(speed_limit_up_kbps=None))
    assert changed.speed_limit_up_kbps is None
    assert changed.speed_limit_down_kbps == 100000
    assert UserPatch(speed_limit_up_kbps=None).model_dump(exclude_unset=True) == {"speed_limit_up_kbps": None}
    assert UserPatch().model_dump(exclude_unset=True) == {}


async def test_template_sync_clears_limits_and_restores_policy(wg_client, panel_transport):
    provider = WGGuardProvider(
        name="test", base_url="http://panel.test", token="wg_test_token", transport=panel_transport
    )
    try:
        reference = await provider.ensure_plan(
            PlanSpec(
                name="first",
                traffic_limit_bytes=100000000000,
                duration_seconds=2592000,
                speed_limit_up_kbps=20000,
                device_limit=1,
            )
        )
        await wg_client.update_plan(reference, PlanPatch(enabled=False, start_policy="immediate"))
        again = await provider.ensure_plan(PlanSpec(name="second", device_limit=1), existing_ref=reference)
        remote = await wg_client.get_plan(again)
        assert again != reference
        assert remote.name == "second" and remote.enabled
        assert remote.start_policy == "first_connection"
        assert remote.traffic_limit_bytes is None and remote.duration_seconds is None
        assert remote.speed_limit_up_kbps is None
        original = await wg_client.get_plan(reference)
        assert original.duration_seconds == 2592000
        assert original.name == "first" and not original.enabled
    finally:
        await provider.aclose()


@pytest.mark.parametrize(("status", "code"), [(503, "NODE_UNAVAILABLE"), (403, "FORBIDDEN"), (404, "USER_NOT_FOUND")])
async def test_recovery_uncertainty_never_becomes_absence(status, code):
    provider = WGGuardProvider(
        name="test",
        base_url="https://node.test",
        token="test-only",
        max_retries=0,
        transport=httpx.MockTransport(
            lambda r: httpx.Response(status, json={"error": {"code": code, "message": "secret-canary"}})
        ),
    )
    try:
        with pytest.raises(PanelError) as error:
            await provider.recover_purchase("order-key")
        assert "secret-canary" not in str(error.value)
    finally:
        await provider.aclose()


async def test_committed_recovery_does_not_need_user_read_scope():
    def respond(request):
        assert request.url.path == "/api/v1/operations/result"
        return httpx.Response(
            200,
            json={
                "operation_id": "op",
                "kind": "purchase",
                "state": "committed",
                "user_id": "u",
                "device_id": "d",
                "template_id": "t",
            },
        )

    provider = WGGuardProvider(
        name="test", base_url="https://node.test", token="test-only", transport=httpx.MockTransport(respond)
    )
    try:
        result = await provider.recover_purchase("key")
        assert result.user_id == "u" and result.recovered
    finally:
        await provider.aclose()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500, 302])
async def test_untrusted_error_bodies_never_reach_errors(status):
    async with WGGuardClient(
        "https://node.test",
        "test-only",
        max_retries=0,
        transport=httpx.MockTransport(lambda r: httpx.Response(status, text="PrivateKey = secret-canary")),
    ) as client:
        with pytest.raises(PanelError) as error:
            await client.node()
    assert "secret-canary" not in str(error.value)
    assert str(status) not in str(error.value)


async def test_retry_after_larger_than_budget_does_not_retry_early():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"error": {"code": "RATE_LIMITED"}})

    async with WGGuardClient(
        "https://node.test", "test-only", transport=httpx.MockTransport(respond), max_backoff=1
    ) as client:
        with pytest.raises(PanelUnavailable):
            await client.node()
    assert len(calls) == 1


async def test_key_rotation_is_never_automatically_retried():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(503, json={"error": {"code": "NODE_UNAVAILABLE"}})

    async with WGGuardClient("https://node.test", "test-only", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(PanelUnavailable):
            await client.regenerate_device("d")
    assert len(calls) == 1


async def test_quota_top_up_preserves_counters_and_recovers_the_same_result(wg_client):
    user = await wg_client.create_user({"username": "topup", "traffic_limit_bytes": 1000000000})
    await wg_client.add_traffic(user.id, rx_bytes=100, tx_bytes=200)
    first = await wg_client.top_up_quota(user.id, 1000000000, idempotency_key="volume-order")
    again = await wg_client.top_up_quota(user.id, 1000000000, idempotency_key="volume-order")
    recovered = await wg_client.operation_result("volume-order")
    assert first.operation_id == again.operation_id == recovered.operation_id
    assert first.before.traffic_limit_bytes == 1000000000
    assert first.after.traffic_limit_bytes == 2000000000
    assert first.after.traffic_used_total == 300
    remote = await wg_client.get_user(user.id)
    assert remote.traffic_limit_bytes == 2000000000 and remote.traffic_used_total == 300


def test_old_wire_routes_and_request_fields_are_rejected(mock_panel):
    from fastapi.testclient import TestClient

    with TestClient(mock_panel) as client:
        assert "/api/v1/plans" not in SPEC["paths"]
        assert client.get("/api/v1/plans", headers={"Authorization": "Bearer wg_test_token"}).status_code == 404
        result = client.post(
            "/api/v1/purchases",
            headers={"Authorization": "Bearer wg_test_token", "Idempotency-Key": "old-wire"},
            json={"plan_id": "plan_starter"},
        )
        assert result.status_code == 400
