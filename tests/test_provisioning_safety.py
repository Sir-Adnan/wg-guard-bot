"""Paid operations under lost responses, concurrent calls and catalog edits."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.core.errors import ConflictError, InsufficientFunds, PanelUnavailable
from app.core.money import GB_DECIMAL, set_gb_basis
from app.db.models import Order, OrderKind, OrderStatus, Payment, PaymentMethod, Service, User
from app.db.session import session_scope
from app.panels.manager import panel_manager
from app.panels.providers.wgguard import WGGuardProvider
from app.services.delivery import delivery
from app.services.orders import order_service
from app.services.provisioning import provisioning
from app.services.users import user_service

pytestmark = pytest.mark.db


async def _service(session, customer, plan):
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    result = await provisioning.provision_order(order.id)
    assert result.ok, result.error
    return await session.get(Service, result.service_id)


async def test_paid_snapshot_survives_catalog_and_unit_changes(session, customer, plan, panel_row, wg_client):
    order = await order_service.create(session, customer, plan, free=True)
    plan.traffic_gb = 50
    plan.duration_days = 90
    plan.speed_limit_down_kbps = 100000
    await session.commit()
    set_gb_basis(decimal=False)
    try:
        result = await provisioning.provision_order(order.id)
        assert result.ok, result.error
        service = await session.get(Service, result.service_id)
        remote = await wg_client.get_user(service.wg_user_id)
        assert remote.traffic_limit_bytes == 30 * GB_DECIMAL
        assert remote.duration_seconds == 30 * 86400
        assert remote.speed_limit_down_kbps is None
    finally:
        set_gb_basis(decimal=True)


async def test_concurrent_provisioning_creates_one_local_and_remote_account(
    session, customer, plan, panel_row, mock_panel
):
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    results = await asyncio.gather(provisioning.provision_order(order.id), provisioning.provision_order(order.id))
    assert all(result.ok for result in results)
    assert results[0].service_id == results[1].service_id
    assert await session.scalar(select(func.count(Service.id))) == 1
    assert len(mock_panel.state.store.users) == 1


async def test_retry_after_username_conflict_and_delivery_failure_reuses_saved_payload(
    session,
    customer,
    plan,
    panel_row,
    mock_panel,
    monkeypatch,
):
    real_purchase = WGGuardProvider.purchase
    calls = []

    async def purchase(self, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            from app.core.errors import PanelConflict

            raise PanelConflict(panel_code="USERNAME_EXISTS")
        return await real_purchase(self, **kwargs)

    real_config = WGGuardProvider.device_config
    failed = False

    async def config(self, device_ref):
        nonlocal failed
        if not failed:
            failed = True
            raise PanelUnavailable()
        return await real_config(self, device_ref)

    monkeypatch.setattr(WGGuardProvider, "purchase", purchase)
    monkeypatch.setattr(WGGuardProvider, "device_config", config)
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    first = await provisioning.provision_order(order.id)
    assert not first.ok
    await session.refresh(order)
    saved = dict(order.meta["purchase"])
    plan.username_template = "different{id}"
    plan.traffic_gb = 99
    await session.commit()
    second = await provisioning.provision_order(order.id)
    assert second.ok, second.error
    await session.refresh(order)
    service = await session.get(Service, second.service_id)
    assert order.meta["purchase"] == saved
    assert service.wg_username == saved["username"]
    assert len(calls) == 2 and len(mock_panel.state.store.users) == 1


async def test_paid_renewal_queues_then_activates_once_at_quota_boundary(session, customer, plan, panel_row, wg_client):
    service = await _service(session, customer, plan)
    before = await wg_client.get_user(service.wg_user_id)
    renew = await order_service.create(session, customer, plan, kind=OrderKind.RENEW, service=service, free=True)
    await session.commit()
    result = await provisioning.provision_order(renew.id)
    assert result.ok, result.error
    await session.refresh(service)
    provider = await panel_manager.provider_for(panel_row)
    queued = await provider.next_plan(service.wg_user_id)
    assert queued is not None and service.auto_renew
    current = await wg_client.get_user(service.wg_user_id)
    assert current.expires_at == before.expires_at and current.traffic_limit_bytes == before.traffic_limit_bytes
    text = await delivery.purchase_success_text(session, service, renew.order_code)
    assert "بسته بعدی" in text
    with pytest.raises(ConflictError):
        await order_service.create(session, customer, plan, kind=OrderKind.RENEW, service=service)
    await wg_client.set_traffic(service.wg_user_id, rx_bytes=before.traffic_limit_bytes, tx_bytes=0)
    await provisioning.sync_service(session, service)
    assert service.traffic_used_bytes == 0 and not service.auto_renew
    assert "paid_next_plan" not in service.meta
    assert await provider.next_plan(service.wg_user_id) is None
    activations = await provider.plan_activations(service.wg_user_id)
    assert len(activations) == 1 and activations[0].plan_ref == queued.plan_ref
    await provisioning.sync_service(session, service)
    assert len(await provider.plan_activations(service.wg_user_id)) == 1


async def test_lost_queue_response_is_reconciled_after_replay_expiry(
    session, customer, plan, panel_row, mock_panel, monkeypatch
):
    service = await _service(session, customer, plan)
    renew = await order_service.create(session, customer, plan, kind=OrderKind.RENEW, service=service, free=True)
    await session.commit()
    real = WGGuardProvider.queue_next_plan
    calls = []

    async def lose_response(self, *args, **kwargs):
        calls.append(kwargs)
        await real(self, *args, **kwargs)
        raise PanelUnavailable()

    monkeypatch.setattr(WGGuardProvider, "queue_next_plan", lose_response)
    first = await provisioning.provision_order(renew.id)
    assert not first.ok
    mock_panel.state.store.config.clock_offset_seconds += 2 * 86400
    second = await provisioning.provision_order(renew.id)
    assert second.ok, second.error
    assert len(calls) == 1


async def test_two_topups_use_remote_allowance_even_with_stale_local_cache(
    session, customer, plan, panel_row, wg_client
):
    service = await _service(session, customer, plan)
    await wg_client.update_user(service.wg_user_id, {"traffic_limit_bytes": 100 * GB_DECIMAL})
    for amount in (5, 10):
        order = await order_service.create(
            session, customer, plan, kind=OrderKind.EXTRA_TRAFFIC, service=service, free=True
        )
        order.traffic_gb = amount
        await session.commit()
        result = await provisioning.provision_order(order.id)
        assert result.ok, result.error
    remote = await wg_client.get_user(service.wg_user_id)
    assert remote.traffic_limit_bytes == 115 * GB_DECIMAL


async def test_insufficient_wallet_does_not_mark_order_paid(session, customer, plan):
    order = await order_service.create(session, customer, plan)
    with pytest.raises(InsufficientFunds):
        await order_service.mark_paid(session, order, method=PaymentMethod.WALLET)
    assert order.status == OrderStatus.PENDING_PAYMENT
    assert await session.scalar(select(func.count(Payment.id))) == 0


async def test_card_payment_never_debits_wallet(session, customer, plan):
    await user_service.credit(session, customer, 5000000)
    order = await order_service.create(session, customer, plan)
    await order_service.mark_paid(session, order, method=PaymentMethod.CARD)
    assert customer.balance_rial == 5000000
    assert await session.scalar(select(func.count(Payment.id))) == 1
    with pytest.raises(ConflictError):
        await order_service.create(session, customer, plan, payment_method=PaymentMethod.CARD, use_wallet=True)


async def test_concurrent_debits_cannot_overspend_or_break_ledger(session, customer):
    await user_service.credit(session, customer, 1000000)
    await session.commit()

    async def spend():
        try:
            async with session_scope() as separate:
                user = await separate.get(User, customer.id)
                await user_service.debit(separate, user, 750000)
            return True
        except InsufficientFunds:
            return False

    outcomes = await asyncio.gather(spend(), spend())
    assert sorted(outcomes) == [False, True]
    await session.refresh(customer)
    total = await session.scalar(select(func.sum(Payment.amount_rial)).where(Payment.user_id == customer.id))
    assert customer.balance_rial == total == 250000


async def test_pending_renewal_cannot_queue_before_payment(session, customer, plan, panel_row):
    service = await _service(session, customer, plan)
    order = await order_service.create(session, customer, plan, kind=OrderKind.RENEW, service=service)
    await session.commit()
    result = await provisioning.provision_order(order.id)
    assert not result.ok
    provider = await panel_manager.provider_for(panel_row)
    assert await provider.next_plan(service.wg_user_id) is None
    await order_service.mark_paid(session, order, method=PaymentMethod.CARD)
    await session.commit()
    assert (await provisioning.provision_order(order.id)).ok
    assert await provider.next_plan(service.wg_user_id) is not None


async def test_old_autorenew_callback_opens_payment_without_free_queue(
    session,
    customer,
    plan,
    panel_row,
    bound_notifier,
    recording_bot,
    feed_update,
):
    from app.bot.callbacks import ServiceCB

    service = await _service(session, customer, plan)
    await session.commit()
    await feed_update(
        recording_bot,
        callback=ServiceCB(action="autorenew", service_id=service.id).pack(),
        telegram_id=customer.telegram_id,
    )
    orders = (await session.execute(select(Order).where(Order.kind == OrderKind.RENEW))).scalars().all()
    assert len(orders) == 1 and orders[0].status == OrderStatus.PENDING_PAYMENT
    assert "بسته بعدی" in " ".join(recording_bot.texts())
    provider = await panel_manager.provider_for(panel_row)
    assert await provider.next_plan(service.wg_user_id) is None


async def test_volume_button_creates_a_topup_order_instead_of_a_new_account(
    session,
    customer,
    plan,
    panel_row,
    bound_notifier,
    recording_bot,
    feed_update,
):
    from app.bot.callbacks import ServiceCB

    service = await _service(session, customer, plan)
    await session.commit()
    await feed_update(
        recording_bot,
        callback=ServiceCB(action="extra", service_id=service.id).pack(),
        telegram_id=customer.telegram_id,
    )
    order = await session.scalar(select(Order).where(Order.kind == OrderKind.EXTRA_TRAFFIC))
    assert order is not None and order.service_id == service.id
    assert order.traffic_gb == plan.traffic_gb and order.payable_rial == plan.price_rial
    assert "خرید حجم اضافه" in " ".join(recording_bot.texts())


async def test_rewards_are_frozen_shared_with_card_flow_and_not_duplicated(session, customer, plan, panel_row):
    from app.services.settings_store import app_settings

    try:
        await app_settings.load(session, force=True)
        await app_settings.set_many(session, {"shop.cashback_percent": 5})
        order = await order_service.create(session, customer, plan)
        await order_service.mark_paid(session, order, method=PaymentMethod.CARD)
        await app_settings.set_many(session, {"shop.cashback_percent": 25})
        await session.commit()
        first = await provisioning.provision_order(order.id)
        second = await provisioning.provision_order(order.id)
        assert first.ok and second.ok
        await session.refresh(customer)
        payments = (await session.execute(select(Payment).where(Payment.order_id == order.id))).scalars().all()
        assert len(payments) == 1 and payments[0].amount_rial == 125000
        assert customer.balance_rial == 125000
    finally:
        await app_settings.reset(session, "shop.cashback_percent")


async def test_unpaid_or_excess_refund_cannot_credit_money(session, customer, plan):
    from app.core.errors import ValidationError

    order = await order_service.create(session, customer, plan)
    with pytest.raises(ValidationError):
        await order_service.refund(session, order, reason="test")
    await order_service.mark_paid(session, order, method=PaymentMethod.CARD)
    with pytest.raises(ValidationError):
        await order_service.refund(session, order, amount_rial=order.payable_rial + 1, reason="test")
    assert customer.balance_rial == 0


async def test_paid_order_cannot_be_canceled_as_an_unpaid_draft(session, customer, plan):
    order = await order_service.create(session, customer, plan)
    await order_service.mark_paid(session, order, method=PaymentMethod.CARD)
    with pytest.raises(ConflictError):
        await order_service.cancel(session, order)
    assert order.status == OrderStatus.PAID


async def test_retries_after_journal_retention_require_manual_reconciliation(
    session, customer, plan, panel_row, mock_panel
):
    from app.core.jalali import now_utc

    order = await order_service.create(session, customer, plan, free=True)
    order.status = OrderStatus.FAILED
    order.attempts = 1
    order.paid_at = now_utc() - timedelta(days=91)
    await session.commit()
    result = await provisioning.provision_order(order.id)
    assert not result.ok and len(mock_panel.state.store.users) == 0


async def test_renewal_activates_on_time_and_waits_while_manually_disabled(
    session, customer, plan, panel_row, wg_client, mock_panel
):
    plan.start_policy = "immediate"
    service = await _service(session, customer, plan)
    renew = await order_service.create(session, customer, plan, kind=OrderKind.RENEW, service=service, free=True)
    await session.commit()
    assert (await provisioning.provision_order(renew.id)).ok
    await wg_client.disable_user(service.wg_user_id)
    mock_panel.state.store.config.clock_offset_seconds += 31 * 86400
    provider = await panel_manager.provider_for(panel_row)
    assert await provider.next_plan(service.wg_user_id) is not None
    assert await provider.plan_activations(service.wg_user_id) == []
    await wg_client.enable_user(service.wg_user_id)
    await wg_client.get_user(service.wg_user_id)
    assert await provider.next_plan(service.wg_user_id) is None
    assert len(await provider.plan_activations(service.wg_user_id)) == 1


async def test_node_capacity_is_enforced_across_concurrent_orders(session, customer, plan, panel_row, mock_panel):
    panel_row.max_services = 1
    orders = [await order_service.create(session, customer, plan, free=True) for _ in range(2)]
    await session.commit()
    results = await asyncio.gather(*(provisioning.provision_order(order.id) for order in orders))
    assert sum(result.ok for result in results) == 1
    assert len(mock_panel.state.store.users) == 1
    await session.refresh(panel_row)
    assert panel_row.service_count == 1


async def test_checkpoint_lock_is_held_and_released_after_cancellation(session):
    from sqlalchemy import text

    from app.db.locks import locked_session
    from app.db.session import get_engine

    ready = asyncio.Event()
    hold = asyncio.Event()

    async def operation():
        async with locked_session(99, 123) as (work, _acquire):
            await work.commit()
            ready.set()
            await hold.wait()

    task = asyncio.create_task(operation())
    await ready.wait()
    async with get_engine().connect() as probe:
        assert not await probe.scalar(text("SELECT pg_try_advisory_lock(99, 123)"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await probe.scalar(text("SELECT pg_try_advisory_lock(99, 123)"))
        await probe.execute(text("SELECT pg_advisory_unlock(99, 123)"))


async def test_topup_lost_after_commit_retries_same_key_without_second_allowance(
    session, customer, plan, panel_row, wg_client, monkeypatch
):
    service = await _service(session, customer, plan)
    initial = service.traffic_limit_bytes
    order = await order_service.create(
        session, customer, plan, kind=OrderKind.EXTRA_TRAFFIC, service=service, free=True
    )
    order.traffic_gb = 10
    await session.commit()
    real = WGGuardProvider.top_up_quota
    keys = []

    async def lose_once(self, user_ref, num_bytes, *, idempotency_key):
        keys.append(idempotency_key)
        remote = await real(self, user_ref, num_bytes, idempotency_key=idempotency_key)
        if len(keys) == 1:
            raise PanelUnavailable()
        return remote

    monkeypatch.setattr(WGGuardProvider, "top_up_quota", lose_once)
    assert not (await provisioning.provision_order(order.id)).ok
    assert (await provisioning.provision_order(order.id)).ok
    assert len(keys) == 2 and keys[0] == keys[1]
    assert (await wg_client.get_user(service.wg_user_id)).traffic_limit_bytes == initial + 10 * GB_DECIMAL


async def test_legacy_paid_device_order_is_not_retried_without_recovery(session, customer, plan, panel_row, wg_client):
    service = await _service(session, customer, plan)
    legacy = await order_service.create(session, customer, plan, free=True)
    legacy.kind = OrderKind.EXTRA_DEVICE
    legacy.service_id = service.id
    await session.commit()
    result = await provisioning.provision_order(legacy.id)
    assert not result.ok
    assert len(await wg_client.list_devices(service.wg_user_id)) == 1


async def test_last_catalog_unit_is_reserved_once_and_released_on_cancel(session, customer, plan):
    from app.core.errors import ValidationError

    plan.is_unlimited_stock = False
    plan.stock = 1
    first = await order_service.create(session, customer, plan)
    assert first.meta["stock_reserved"] and plan.stock == 0
    with pytest.raises(ValidationError):
        await order_service.create(session, customer, plan)
    await session.refresh(plan)
    assert plan.stock == 0
    await order_service.cancel(session, first)
    await session.refresh(plan)
    assert plan.stock == 1
    await order_service.cancel(session, first)
    await session.refresh(plan)
    assert plan.stock == 1


async def test_checkout_failure_does_not_leave_stock_or_a_partial_order(session, customer, plan):
    from app.core.errors import ValidationError

    plan.is_unlimited_stock = False
    plan.stock = 1
    with pytest.raises(ValidationError):
        await order_service.create(session, customer, plan, discount_code="missing-coupon")
    await session.refresh(plan)
    assert plan.stock == 1
    assert await session.scalar(select(func.count(Order.id))) == 0


async def test_success_does_not_decrement_reserved_catalog_stock_twice(session, customer, plan, panel_row):
    plan.is_unlimited_stock = False
    plan.stock = 2
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    assert (await provisioning.provision_order(order.id)).ok
    await session.refresh(plan)
    assert plan.stock == 1 and plan.sales_count == 1


async def test_expired_checkout_releases_stock_and_refunds_wallet_once(session, customer, plan):
    from app.core.jalali import now_utc

    plan.is_unlimited_stock = False
    plan.stock = 1
    await user_service.credit(session, customer, 1000000)
    order = await order_service.create(session, customer, plan, use_wallet=True)
    order.payment_deadline = now_utc() - timedelta(minutes=1)
    await session.flush()
    expired = await order_service.expire_stale(session)
    assert [row.id for row in expired] == [order.id]
    await session.refresh(plan)
    assert plan.stock == 1 and customer.balance_rial == 1000000
    await order_service.cancel(session, order)
    assert customer.balance_rial == 1000000
    with pytest.raises(ConflictError):
        await order_service.mark_paid(session, order, method=PaymentMethod.WALLET)
    assert await session.scalar(select(func.count(Payment.id))) == 3


async def test_receipt_under_review_does_not_expire_during_operator_delay(session, customer, plan):
    from app.core.jalali import now_utc

    order = await order_service.create(session, customer, plan)
    await order_service.mark_awaiting_review(session, order)
    order.payment_deadline = now_utc() - timedelta(days=1)
    assert await order_service.expire_stale(session) == []
    assert order.status == OrderStatus.AWAITING_REVIEW


def test_random_idempotency_key_is_not_mistaken_for_a_retry_suffix():
    from types import SimpleNamespace

    for key in ("wggb-random-original", "wggb-alpha-random", "wggb-original-r1"):
        order = SimpleNamespace(idempotency_key=key, meta={})
        assert provisioning._idem_base(order) == key
        assert order.meta["idem_base"] == key
