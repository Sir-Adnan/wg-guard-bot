"""A recovered atomic purchase must retain and deliver every created device."""

import pytest
from sqlalchemy import func, select

from app.core.errors import PanelUnavailable
from app.core.security import decrypt_secret
from app.db.models import Service, ServiceDevice
from app.panels.providers.wgguard import WGGuardProvider
from app.services.delivery import delivery
from app.services.orders import order_service
from app.services.provisioning import provisioning

pytestmark = pytest.mark.db


async def test_multi_device_recovery_after_config_failure_keeps_all_configs(
    session,
    customer,
    plan,
    panel_row,
    mock_panel,
    monkeypatch,
    bound_notifier,
    recording_bot,
):
    plan.device_limit = 3
    purchase = WGGuardProvider.purchase
    purchase_calls = []

    async def multi_purchase(self, **kwargs):
        purchase_calls.append(kwargs)
        return await purchase(self, **kwargs, device_count=3)

    monkeypatch.setattr(WGGuardProvider, "purchase", multi_purchase)
    get_config = WGGuardProvider.device_config
    config_calls = []

    async def fail_last_once(self, device_id):
        config_calls.append(device_id)
        if len(config_calls) == 3:
            raise PanelUnavailable()
        return await get_config(self, device_id)

    monkeypatch.setattr(WGGuardProvider, "device_config", fail_last_once)
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    assert not (await provisioning.provision_order(order.id)).ok
    assert await session.scalar(select(func.count(ServiceDevice.id))) == 0
    result = await provisioning.provision_order(order.id)
    assert result.ok, result.error
    service = await session.get(Service, result.service_id)
    assert len(purchase_calls) == 1 and len(mock_panel.state.store.users) == 1
    assert len(service.devices) == len(mock_panel.state.store.devices) == 3
    assert {device.wg_device_id for device in service.devices} == set(mock_panel.state.store.devices)
    assert {device.name for device in service.devices} == {"device-1-1", "device-1-2", "device-1-3"}
    assert all(device.ipv4_address for device in service.devices)
    configs = [decrypt_secret(device.config_encrypted, purpose="config") for device in service.devices]
    assert len(set(configs)) == 3 and all("[Interface]" in config for config in configs)
    assert all(device.config_encrypted != config for device, config in zip(service.devices, configs, strict=True))
    recording_bot.calls.clear()
    assert await delivery.deliver_service(session, service)
    assert sum(name == "send_document" for name, kwargs in recording_bot.calls) == 3
    assert sum(name == "send_photo" for name, kwargs in recording_bot.calls) == 1
