"""Self-contained smoke tests for the in-memory WG-Guard panel mock.

Run from the repository root::

    .venv\\Scripts\\python.exe -m pytest tools/mock_wg_panel/test_mock_smoke.py -q
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

# The package lives in a directory without an ``__init__.py`` (tools/), so make it importable.
_PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(_PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(_PACKAGE_PARENT))

from mock_wg_panel import MockConfig, create_app  # noqa: E402
from mock_wg_panel.store import DEFAULT_TOKEN, READONLY_TOKEN  # noqa: E402

AUTH = {"Authorization": f"Bearer {DEFAULT_TOKEN}"}
READONLY_AUTH = {"Authorization": f"Bearer {READONLY_TOKEN}"}

CONFIG_MARKERS = (
    "[Interface]",
    "PrivateKey",
    "Address",
    "DNS",
    "MTU",
    "Jc",
    "Jmin",
    "Jmax",
    "S1",
    "S2",
    "H1",
    "H2",
    "H3",
    "H4",
    "[Peer]",
    "PublicKey",
    "AllowedIPs",
    "Endpoint",
    "PersistentKeepalive",
)


@pytest.fixture()
def client() -> Any:
    """A TestClient over a freshly seeded mock panel."""
    with TestClient(create_app()) as test_client:
        yield test_client


def provision(client: TestClient, key: str, username: str = "alice") -> tuple[str, str]:
    """Create a purchase and return ``(user_id, device_id)``."""
    response = client.post(
        "/api/v1/purchases",
        json={"plan_id": "plan_starter", "username": username, "device_name": "phone"},
        headers={**AUTH, "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()["user_id"], response.json()["device_id"]


# --------------------------------------------------------------------------- #
# public surface and authentication
# --------------------------------------------------------------------------- #
def test_public_endpoints_need_no_token(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").status_code == 200
    node_health = client.get("/api/v1/node/health")
    assert node_health.status_code == 200 and node_health.json()["version"]
    assert client.get("/openapi.json").status_code == 200


def test_missing_token_returns_401_envelope(client: TestClient) -> None:
    for path in ("/api/v1/node", "/api/v1/users", "/api/v1/stats"):
        response = client.get(path)
        assert response.status_code == 401
        error = response.json()["error"]
        assert error["code"] == "UNAUTHORIZED"
        assert error["message"] and error["request_id"]


def test_unknown_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/v1/node", headers={"Authorization": "Bearer wg_nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHORIZED"


def test_missing_scope_returns_403(client: TestClient) -> None:
    assert client.get("/api/v1/node", headers=READONLY_AUTH).status_code == 200
    forbidden = client.post(
        "/api/v1/purchases",
        json={"plan_id": "plan_starter"},
        headers={**READONLY_AUTH, "Idempotency-Key": "scope-key"},
    )
    assert forbidden.status_code == 403
    error = forbidden.json()["error"]
    assert error["code"] == "FORBIDDEN" and "purchases.create" in error["message"]


def test_custom_tokens_from_config() -> None:
    config = MockConfig(tokens={"wg_only_users": frozenset({"users.read"})})
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/users", headers={"Authorization": "Bearer wg_only_users"}).status_code == 200
        denied = client.get("/api/v1/plans", headers={"Authorization": "Bearer wg_only_users"})
        assert denied.status_code == 403
        assert client.get("/api/v1/users", headers=READONLY_AUTH).status_code == 401


# --------------------------------------------------------------------------- #
# purchases, idempotency, operations
# --------------------------------------------------------------------------- #
def test_purchase_creates_user_device_and_link(client: TestClient) -> None:
    response = client.post(
        "/api/v1/purchases",
        json={"plan_id": "plan_starter", "username": "alice", "device_name": "phone"},
        headers={**AUTH, "Idempotency-Key": "purchase-basic"},
    )
    assert response.status_code == 201
    result = response.json()
    assert result["kind"] == "purchase" and result["state"] == "committed"
    assert result["plan_id"] == "plan_starter" and result["operation_id"]
    user = client.get(f"/api/v1/users/{result['user_id']}", headers=AUTH).json()
    assert user["username"] == "alice" and user["plan_id"] == "plan_starter"
    assert user["traffic_limit_bytes"] == 32 * 1024**3 and user["expires_at"]
    devices = client.get(f"/api/v1/users/{result['user_id']}/devices", headers=AUTH).json()["items"]
    assert [device["id"] for device in devices] == [result["device_id"]]
    assert devices[0]["name"] == "phone" and devices[0]["ipv4_address"].startswith("10.8.0.")
    link = client.get(f"/api/v1/users/{result['user_id']}/subscription", headers=AUTH).json()
    assert link["path"].startswith("/sub/") and len(link["path"]) > 10


def test_purchase_replay_and_operations_result(client: TestClient) -> None:
    body = {"plan_id": "plan_standard", "username": "replay-user", "device_name": "laptop"}
    headers = {**AUTH, "Idempotency-Key": "purchase-replay"}
    first = client.post("/api/v1/purchases", json=body, headers=headers)
    assert first.status_code == 201 and "Idempotency-Replayed" not in first.headers

    second = client.post("/api/v1/purchases", json=body, headers=headers)
    assert second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotency-Replayed"] == "true"

    journal = client.get("/__mock__/requests").json()
    keys = [entry["headers"].get("idempotency-key") for entry in journal if entry["path"] == "/api/v1/purchases"]
    assert keys == ["purchase-replay", "purchase-replay"]

    recovered = client.get("/api/v1/operations/result", headers=headers)
    assert recovered.status_code == 200 and recovered.json() == first.json()
    assert client.get("/api/v1/operations/result", headers={**AUTH, "Idempotency-Key": "never-used"}).status_code == 404


def test_same_key_different_body_conflicts(client: TestClient) -> None:
    headers = {**AUTH, "Idempotency-Key": "purchase-conflict"}
    assert (
        client.post(
            "/api/v1/purchases", json={"plan_id": "plan_starter", "username": "bob"}, headers=headers
        ).status_code
        == 201
    )
    clash = client.post("/api/v1/purchases", json={"plan_id": "plan_standard", "username": "bob"}, headers=headers)
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "CONFLICT"


def test_purchase_validation(client: TestClient) -> None:
    missing = client.post("/api/v1/purchases", json={"plan_id": "plan_starter"}, headers=AUTH)
    assert missing.status_code == 400 and missing.json()["error"]["code"] == "INVALID_REQUEST"

    spaced = client.post(
        "/api/v1/purchases", json={"plan_id": "plan_starter"}, headers={**AUTH, "Idempotency-Key": "bad key"}
    )
    assert spaced.status_code == 400 and spaced.json()["error"]["code"] == "INVALID_REQUEST"

    unknown_key = client.post(
        "/api/v1/purchases",
        json={"plan_id": "plan_starter", "surprise": True},
        headers={**AUTH, "Idempotency-Key": "extra-key"},
    )
    assert unknown_key.status_code == 400 and unknown_key.json()["error"]["code"] == "INVALID_REQUEST"

    unknown_plan = client.post(
        "/api/v1/purchases",
        json={"plan_id": "plan-does-not-exist"},
        headers={**AUTH, "Idempotency-Key": "unknown-plan"},
    )
    assert unknown_plan.status_code == 404 and unknown_plan.json()["error"]["code"] == "PLAN_NOT_FOUND"

    duplicate = client.post(
        "/api/v1/purchases",
        json={"plan_id": "plan_starter", "username": "alice"},
        headers={**AUTH, "Idempotency-Key": "dupe-1"},
    )
    assert duplicate.status_code == 201
    clash = client.post(
        "/api/v1/purchases",
        json={"plan_id": "plan_starter", "username": "alice"},
        headers={**AUTH, "Idempotency-Key": "dupe-2"},
    )
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "USERNAME_EXISTS"


def test_purchase_derives_stable_username(client: TestClient) -> None:
    headers = {**AUTH, "Idempotency-Key": "derived-key"}
    first = client.post("/api/v1/purchases", json={"plan_id": "plan_starter"}, headers=headers).json()
    user = client.get(f"/api/v1/users/{first['user_id']}", headers=AUTH).json()
    assert user["username"].startswith("u_") and len(user["username"]) == 14


# --------------------------------------------------------------------------- #
# users, pagination, filters
# --------------------------------------------------------------------------- #
def test_user_pagination_and_filters(client: TestClient) -> None:
    for index in range(5):
        created = client.post("/api/v1/users", json={"username": f"pager{index:02d}"}, headers=AUTH)
        assert created.status_code == 201, created.text

    first = client.get("/api/v1/users", params={"limit": 2}, headers=AUTH).json()
    assert len(first["items"]) == 2 and first["next_cursor"]
    second = client.get("/api/v1/users", params={"limit": 2, "cursor": first["next_cursor"]}, headers=AUTH).json()
    assert len(second["items"]) == 2 and second["next_cursor"]
    third = client.get("/api/v1/users", params={"limit": 2, "cursor": second["next_cursor"]}, headers=AUTH).json()
    assert len(third["items"]) == 1 and third["next_cursor"] == ""
    ids = [user["id"] for user in first["items"] + second["items"] + third["items"]]
    assert len(set(ids)) == 5

    filtered = client.get("/api/v1/users", params={"username": "pager00", "enabled": True}, headers=AUTH).json()
    assert [user["username"] for user in filtered["items"]] == ["pager00"]
    assert len(client.get("/api/v1/users", params={"username": "pager0"}, headers=AUTH).json()["items"]) == 5
    assert client.get("/api/v1/users", params={"limit": 9999}, headers=AUTH).status_code == 200
    assert client.get("/api/v1/users", params={"cursor": "not-a-cursor"}, headers=AUTH).status_code == 400


def test_user_lifecycle(client: TestClient) -> None:
    user_id, _ = provision(client, "lifecycle-key", username="cycle")
    assert (
        client.post(f"/api/v1/users/{user_id}/disable", json={"reason": "manual"}, headers=AUTH).json()["status"]
        == "disabled"
    )
    assert client.post(f"/api/v1/users/{user_id}/enable", headers=AUTH).json()["enabled"] is True

    added = client.post(f"/api/v1/users/{user_id}/traffic/add", json={"rx_bytes": 1024, "tx_bytes": 2048}, headers=AUTH)
    assert added.json()["traffic_used_total"] == 3072
    set_result = client.post(
        f"/api/v1/users/{user_id}/traffic/set", json={"rx_bytes": 5, "tx_bytes": None}, headers=AUTH
    )
    assert set_result.json()["traffic_used_rx"] == 5 and set_result.json()["traffic_used_tx"] == 2048
    assert client.post(f"/api/v1/users/{user_id}/traffic/reset", headers=AUTH).json()["traffic_used_total"] == 0

    renewed = client.post(
        f"/api/v1/users/{user_id}/renew", json={"mode": "from_now", "duration_seconds": 3600}, headers=AUTH
    )
    assert renewed.status_code == 200 and renewed.json()["expires_at"]

    patched = client.patch(f"/api/v1/users/{user_id}", json={"note": "vip", "traffic_limit_bytes": None}, headers=AUTH)
    assert patched.json()["note"] == "vip" and patched.json()["traffic_limit_bytes"] is None
    assert client.patch(f"/api/v1/users/{user_id}", json={"username": "other"}, headers=AUTH).status_code == 400

    assert client.delete(f"/api/v1/users/{user_id}", headers=AUTH).status_code == 200
    assert client.get(f"/api/v1/users/{user_id}", headers=AUTH).json()["deleted"] is True
    assert client.get(f"/api/v1/users/{user_id}/stats", headers=AUTH).json()["status"] == "disabled"
    assert client.get("/api/v1/users/usr_missing", headers=AUTH).status_code == 404


def test_user_create_is_idempotent_and_limit_trips(client: TestClient) -> None:
    headers = {**AUTH, "Idempotency-Key": "user-create-key"}
    body = {"username": "idem-user", "traffic_limit_bytes": 1000, "duration_seconds": 60}
    first = client.post("/api/v1/users", json=body, headers=headers)
    assert first.status_code == 201
    assert client.post("/api/v1/users", json=body, headers=headers).headers["Idempotency-Replayed"] == "true"
    assert client.post("/api/v1/users", json={**body, "note": "changed"}, headers=headers).status_code == 409

    user_id = first.json()["id"]
    tripped = client.post(f"/api/v1/users/{user_id}/traffic/add", json={"rx_bytes": 1000}, headers=AUTH).json()
    assert tripped["status"] == "traffic_exceeded" and tripped["enabled"] is False
    assert (
        client.get("/api/v1/users", params={"traffic_exceeded": True}, headers=AUTH).json()["items"][0]["id"] == user_id
    )
    assert (
        client.get("/api/v1/users", params={"status": "traffic_exceeded"}, headers=AUTH).json()["items"][0]["id"]
        == user_id
    )


def test_mock_expire_forces_expired_status(client: TestClient) -> None:
    user_id, _ = provision(client, "expire-key", username="soon-expired")
    forced = client.post("/__mock__/expire", json={"user_id": user_id})
    assert forced.status_code == 200 and forced.json()["status"] == "expired"
    user = client.get(f"/api/v1/users/{user_id}", headers=AUTH).json()
    assert user["status"] == "expired" and user["enabled"] is False and user["disable_reason"] == "expired"
    assert client.post("/__mock__/expire", json={"user_id": "usr_nope"}).status_code == 404


# --------------------------------------------------------------------------- #
# devices, configuration, QR
# --------------------------------------------------------------------------- #
def test_config_text_json_and_qr(client: TestClient) -> None:
    _, device_id = provision(client, "config-key", username="config-user")
    response = client.get(f"/api/v1/devices/{device_id}/config", headers=AUTH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    for marker in CONFIG_MARKERS:
        assert marker in text, marker
    assert text.endswith("\n") and not text.endswith("\n\n")
    assert device_id not in text  # the private key is the only secret carried

    as_json = client.get(f"/api/v1/devices/{device_id}/config", params={"format": "json"}, headers=AUTH)
    assert as_json.json()["config"] == text

    qr = client.get(f"/api/v1/devices/{device_id}/qr", headers=AUTH)
    assert qr.status_code == 200
    assert qr.headers["content-type"] == "image/png"
    assert qr.content.startswith(b"\x89PNG\r\n\x1a\n") and qr.content.endswith(b"IEND\xaeB`\x82")

    assert client.get("/api/v1/devices/dev_missing/config", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/devices/{device_id}/config", headers=READONLY_AUTH).status_code == 200


def test_device_crud_and_rotation(client: TestClient) -> None:
    user_id, device_id = provision(client, "device-key", username="device-user")
    created = client.post(f"/api/v1/users/{user_id}/devices", json={"name": "tablet"}, headers=AUTH)
    assert created.status_code == 201 and created.json()["ipv4_address"] != ""
    assert client.post(f"/api/v1/users/{user_id}/devices", json={"name": "tablet"}, headers=AUTH).status_code == 409
    # plan_starter allows two devices
    assert client.post(f"/api/v1/users/{user_id}/devices", json={"name": "third"}, headers=AUTH).status_code == 409

    renamed = client.patch(f"/api/v1/devices/{device_id}", json={"name": "phone-2"}, headers=AUTH)
    assert renamed.json()["name"] == "phone-2"
    assert client.post(f"/api/v1/devices/{device_id}/disable", headers=AUTH).json()["enabled"] is False
    assert client.post(f"/api/v1/devices/{device_id}/enable", headers=AUTH).json()["enabled"] is True

    before = client.get(f"/api/v1/devices/{device_id}/config", headers=AUTH).text
    regenerated = client.post(f"/api/v1/devices/{device_id}/regenerate", json={"preshared_key": True}, headers=AUTH)
    assert regenerated.status_code == 200
    assert client.get(f"/api/v1/devices/{device_id}/config", headers=AUTH).text != before

    rotated = client.post(f"/api/v1/users/{user_id}/subscription/rotate", headers=AUTH).json()
    assert rotated["devices_rotated"] == 2 and rotated["path"].startswith("/sub/")
    assert client.delete(f"/api/v1/devices/{device_id}", headers=AUTH).status_code == 200
    assert client.get(f"/api/v1/devices/{device_id}", headers=AUTH).status_code == 404


# --------------------------------------------------------------------------- #
# node, stats, plans, interfaces, settings, webhooks
# --------------------------------------------------------------------------- #
def test_node_stats_telemetry_and_settings(client: TestClient) -> None:
    provision(client, "node-key", username="node-user")
    node = client.get("/api/v1/node", headers=AUTH).json()
    assert node["node_id"] and node["interfaces"][0]["devices"] == 1

    stats = client.get("/api/v1/node/stats", headers=AUTH).json()
    assert stats["users_total"] == 1 and stats["devices_total"] == 1
    assert client.get("/api/v1/stats", headers=AUTH).json()["traffic"]["total_bytes"] == 0

    telemetry = client.get("/api/v1/node/telemetry", params={"points": 5}, headers=AUTH).json()
    assert telemetry["cadence_seconds"] >= 1 and len(telemetry["points"]) == 5
    assert telemetry["latest"] == telemetry["points"][-1]
    assert telemetry["points"][0]["timestamp"] < telemetry["points"][-1]["timestamp"]
    assert telemetry["points"][-1]["health"] in ("healthy", "degraded", "unavailable")
    assert len(client.get("/api/v1/node/telemetry", params={"points": 9999}, headers=AUTH).json()["points"]) == 180
    assert client.get("/api/v1/node/telemetry", params={"points": "lots"}, headers=AUTH).status_code == 400

    settings = client.get("/api/v1/settings", headers=AUTH).json()
    assert any(item["key"] == "node.endpoint" for item in settings["items"])
    patched = client.patch("/api/v1/settings", json={"node.endpoint": "vpn.other.test"}, headers=AUTH).json()
    assert "node.endpoint" in patched["updated"]
    assert patched["settings"]["node.endpoint"] == "vpn.other.test"
    assert client.patch("/api/v1/settings", json={"api.rate_limit_per_minute": "many"}, headers=AUTH).status_code == 400


def test_plans_and_interfaces_crud(client: TestClient) -> None:
    plans = client.get("/api/v1/plans", headers=AUTH).json()["items"]
    assert {"plan_starter", "plan_standard"} <= {plan["id"] for plan in plans}

    provision(client, "plan-assign-key", username="plan-user")  # plan_starter becomes assigned
    created = client.post("/api/v1/plans", json={"name": "Custom", "device_limit": 1}, headers=AUTH)
    assert created.status_code == 201
    plan_id = created.json()["id"]
    assert client.patch(f"/api/v1/plans/{plan_id}", json={"enabled": False}, headers=AUTH).json()["enabled"] is False
    assert client.delete(f"/api/v1/plans/{plan_id}", headers=AUTH).status_code == 200
    assert client.delete("/api/v1/plans/plan_starter", headers=AUTH).status_code == 409  # assigned to a user

    seeded = client.post("/__mock__/seed/plan", json={"id": "plan_e2e", "name": "E2E", "device_limit": 9})
    assert seeded.status_code == 200 and seeded.json()["id"] == "plan_e2e"

    interfaces = client.get("/api/v1/interfaces", headers=AUTH).json()["items"]
    assert interfaces and any(iface["name"] == "awg0" for iface in interfaces)
    new_iface = client.post("/api/v1/interfaces", json={"name": "awg7", "listen_port": 51999}, headers=AUTH)
    assert new_iface.status_code == 201
    iface_id = new_iface.json()["id"]
    assert client.get(f"/api/v1/interfaces/{iface_id}", headers=AUTH).json()["listen_port"] == 51999
    patched = client.patch(f"/api/v1/interfaces/{iface_id}", json={"mtu": 1300}, headers=AUTH).json()
    assert patched["mtu"] == 1300
    assert client.delete(f"/api/v1/interfaces/{iface_id}", headers=AUTH).status_code == 200
    assert client.post("/api/v1/interfaces", json={"name": "awg0"}, headers=AUTH).status_code == 409
    assert client.post("/api/v1/interfaces", json={"name": "wg0"}, headers=AUTH).status_code == 400


def test_user_and_device_statistics(client: TestClient) -> None:
    user_id, device_id = provision(client, "stats-key", username="stats-user")
    client.post(f"/api/v1/users/{user_id}/traffic/add", json={"rx_bytes": 50, "tx_bytes": 50}, headers=AUTH)
    user_stats = client.get(f"/api/v1/users/{user_id}/stats", headers=AUTH).json()
    assert user_stats["used_bytes"] == 100 and user_stats["percent"] is not None
    device_stats = client.get(f"/api/v1/devices/{device_id}/stats", headers=AUTH).json()
    assert device_stats["total_bytes"] == 0 and device_stats["online"] is False
    series = client.get(
        f"/api/v1/users/{user_id}/traffic", params={"granularity": "hourly", "hours": 4}, headers=AUTH
    ).json()
    assert series["granularity"] == "hourly" and sum(point["rx"] for point in series["series"]) == 50


def test_webhooks_flow(client: TestClient) -> None:
    created = client.post(
        "/api/v1/webhooks",
        json={"url": "https://receiver.test/hook", "events": ["user.created", "device.created"]},
        headers=AUTH,
    )
    assert created.status_code == 201
    webhook_id = created.json()["id"]
    assert created.json()["secret"] and created.json()["stats"]["delivered"] == 1

    fetched = client.get(f"/api/v1/webhooks/{webhook_id}", headers=AUTH).json()
    assert fetched["events"] == ["user.created", "device.created"] and "secret" not in fetched
    assert (
        client.patch(f"/api/v1/webhooks/{webhook_id}", json={"enabled": False}, headers=AUTH).json()["enabled"] is False
    )
    assert (
        client.post("/api/v1/webhooks", json={"url": "https://x.test", "events": ["nope"]}, headers=AUTH).status_code
        == 400
    )

    deliveries = client.get(f"/api/v1/webhooks/{webhook_id}/deliveries", headers=AUTH).json()
    assert deliveries["items"] and deliveries["next_before"] == deliveries["items"][-1]["id"]
    receipt = client.get(f"/api/v1/webhooks/{webhook_id}/deliveries/{deliveries['items'][0]['id']}", headers=AUTH)
    assert receipt.status_code == 200 and receipt.json()["status"] in ("pending", "delivered", "dead")
    redelivered = client.post(
        f"/api/v1/webhooks/{webhook_id}/redeliver", json={"delivery_id": deliveries["items"][0]["id"]}, headers=AUTH
    )
    assert redelivered.status_code == 202 and redelivered.json()["status"] == "pending"
    assert client.get("/api/v1/webhooks/wh_missing", headers=AUTH).status_code == 404
    assert client.delete(f"/api/v1/webhooks/{webhook_id}", headers=AUTH).status_code == 200


# --------------------------------------------------------------------------- #
# test affordances
# --------------------------------------------------------------------------- #
def test_fail_injection_retries_then_succeeds(client: TestClient) -> None:
    armed = client.post(
        "/__mock__/fail",
        json={
            "path": "/api/v1/purchases",
            "count": 2,
            "status": 503,
            "body": {"error": {"code": "NODE_UNAVAILABLE", "message": "runtime reconciliation pending"}},
        },
    )
    assert armed.status_code == 200 and armed.json()["count"] == 2

    headers = {**AUTH, "Idempotency-Key": "retry-key"}
    body = {"plan_id": "plan_starter", "username": "retry-user"}
    for _ in range(2):
        failed = client.post("/api/v1/purchases", json=body, headers=headers)
        assert failed.status_code == 503
        assert failed.json()["error"]["code"] == "NODE_UNAVAILABLE"
        assert failed.json()["error"]["request_id"]
    assert client.post("/api/v1/purchases", json=body, headers=headers).status_code == 201
    assert client.get("/api/v1/users", headers=AUTH).json()["items"][0]["username"] == "retry-user"


def test_slow_injection_delays_matching_requests(client: TestClient) -> None:
    client.post("/__mock__/slow", json={"path": "/healthz", "seconds": 0.3, "count": 1})
    started = time.monotonic()
    assert client.get("/healthz").status_code == 200
    assert time.monotonic() - started >= 0.25
    started = time.monotonic()
    client.get("/healthz")
    assert time.monotonic() - started < 0.25


def test_request_journal_and_reset(client: TestClient) -> None:
    provision(client, "journal-key", username="journal-user")
    journal = client.get("/__mock__/requests").json()
    assert journal[0]["method"] == "POST" and journal[0]["path"] == "/api/v1/purchases"
    assert journal[0]["headers"]["authorization"] == f"Bearer {DEFAULT_TOKEN}"
    assert journal[0]["body"]["username"] == "journal-user"

    assert client.post("/__mock__/reset").json()["ok"] is True
    # the reset itself is wiped from the journal, leaving a clean slate
    remaining = [r for r in client.get("/__mock__/requests").json() if not r["path"].startswith("/__mock__")]
    assert remaining == []
    assert client.get("/api/v1/users", headers=AUTH).json()["items"] == []
    # the seed is restored: the same purchase key is usable again
    assert (
        client.post(
            "/api/v1/purchases",
            json={"plan_id": "plan_starter", "username": "journal-user"},
            headers={**AUTH, "Idempotency-Key": "journal-key"},
        ).status_code
        == 201
    )


# --------------------------------------------------------------------------- #
# async transport parity
# --------------------------------------------------------------------------- #
async def _async_flow(app: Any) -> tuple[int, str, int]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock") as async_client:
        health = await async_client.get("/healthz")
        unauthorized = await async_client.get("/api/v1/users")
        purchase = await async_client.post(
            "/api/v1/purchases",
            json={"plan_id": "plan_unlimited", "username": "async-user"},
            headers={**AUTH, "Idempotency-Key": "async-key"},
        )
        return health.status_code, unauthorized.json()["error"]["code"], purchase.status_code


def test_httpx_asgi_transport_parity() -> None:
    app = create_app()
    assert asyncio.run(_async_flow(app)) == (200, "UNAUTHORIZED", 201)
