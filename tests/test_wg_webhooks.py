"""Webhook authentication, durable dedupe and unordered reconciliation."""

import asyncio
import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import func, select
from starlette.requests import Request

from app.core.errors import PanelUnavailable
from app.core.security import encrypt_secret
from app.db.models import Service, ServiceStatus, WebhookEvent
from app.db.session import session_scope
from app.panels.providers.wgguard import WGGuardProvider
from app.services.orders import order_service
from app.services.provisioning import provisioning
from app.web.routes.webhook import handle_wg_event, verify_wg_signature

SECRET = "test-webhook-secret"


def _signature(body, secret=SECRET, timestamp=None):
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    mac = hmac.new(secret.encode(), stamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"t={stamp},v1={mac}"


def _request(body):
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/wg/webhook/1", "headers": []}, receive)


async def _send(session, panel, body, secret=SECRET):
    return await handle_wg_event(
        panel.id, _request(body), session, signature=_signature(body, secret), delivery=None, event_header=None
    )


def test_signature_requires_exact_bytes_and_unix_seconds():
    body = b'{"id":"e","type":"user.updated"}'
    assert verify_wg_signature(SECRET, _signature(body), body)
    assert not verify_wg_signature(SECRET, _signature(body), body + b" ")
    assert not verify_wg_signature(SECRET, _signature(body, timestamp=int(time.time()) * 1000), body)
    assert not verify_wg_signature(SECRET, _signature(body, timestamp=int(time.time()) - 301), body)
    assert not verify_wg_signature(SECRET, _signature(body) + ",t=1", body)
    assert not verify_wg_signature(SECRET, "t=1,v1=غ", body)


@pytest.mark.db
async def test_api_token_is_not_a_webhook_secret(session, panel_row):
    result = await _send(session, panel_row, b'{"id":"e","type":"node.started","data":{}}', secret="wg_test_token")
    assert result.status_code == 401


@pytest.mark.db
@pytest.mark.parametrize("payload", [[], "text", {"id": "e", "type": "x", "data": []}, {"type": "x", "data": {}}])
async def test_signed_malformed_json_is_rejected(session, panel_row, payload):
    panel_row.webhook_secret_encrypted = encrypt_secret(SECRET, purpose="webhook-secret")
    await session.commit()
    result = await _send(session, panel_row, json.dumps(payload).encode())
    assert result.status_code == 400
    assert await session.scalar(select(func.count(WebhookEvent.id))) == 0


@pytest.mark.db
async def test_delayed_expiry_event_reads_current_resource_instead_of_expiring_it(session, customer, plan, panel_row):
    panel_row.webhook_secret_encrypted = encrypt_secret(SECRET, purpose="webhook-secret")
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    created = await provisioning.provision_order(order.id)
    assert created.ok
    service = await session.get(Service, created.service_id)
    body = json.dumps(
        {
            "id": "old-event",
            "type": "user.expired",
            "data": {"user_id": service.wg_user_id, "unexpected_secret": "secret-canary"},
        }
    ).encode()
    result = await _send(session, panel_row, body)
    assert result.status_code == 200
    await session.refresh(service)
    assert service.status == ServiceStatus.ACTIVE
    rows = (await session.execute(select(WebhookEvent))).scalars().all()
    assert len(rows) == 1 and rows[0].processed_at is not None
    assert "secret-canary" not in json.dumps(rows[0].payload)
    assert (await _send(session, panel_row, body)).status_code == 200
    assert await session.scalar(select(func.count(WebhookEvent.id))) == 1


@pytest.mark.db
async def test_failed_reconciliation_is_not_acknowledged_or_deduplicated(
    session, customer, plan, panel_row, monkeypatch
):
    panel_row.webhook_secret_encrypted = encrypt_secret(SECRET, purpose="webhook-secret")
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    created = await provisioning.provision_order(order.id)
    service = await session.get(Service, created.service_id)
    body = json.dumps({"id": "retry-event", "type": "user.updated", "data": {"user_id": service.wg_user_id}}).encode()
    real = WGGuardProvider.get_user

    async def unavailable(self, user_id):
        raise PanelUnavailable()

    monkeypatch.setattr(WGGuardProvider, "get_user", unavailable)
    assert (await _send(session, panel_row, body)).status_code == 503
    assert await session.scalar(select(func.count(WebhookEvent.id))) == 0
    await session.refresh(panel_row)
    monkeypatch.setattr(WGGuardProvider, "get_user", real)
    assert (await _send(session, panel_row, body)).status_code == 200
    assert await session.scalar(select(func.count(WebhookEvent.id))) == 1


@pytest.mark.db
async def test_concurrent_redelivery_commits_one_event(session, panel_row):
    panel_row.webhook_secret_encrypted = encrypt_secret(SECRET, purpose="webhook-secret")
    await session.commit()
    panel_id = panel_row.id
    body = b'{"id":"concurrent","type":"future.signal","data":{}}'

    async def deliver():
        from app.db.models import Panel

        async with session_scope() as separate:
            panel = await separate.get(Panel, panel_id)
            return await _send(separate, panel, body)

    results = await asyncio.gather(deliver(), deliver())
    assert [r.status_code for r in results] == [200, 200]
    assert await session.scalar(select(func.count(WebhookEvent.id))) == 1
