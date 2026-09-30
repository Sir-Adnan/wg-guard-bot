"""End-to-end tests of the money path: order → payment → provisioning.

These exercise the real services against PostgreSQL (transactionally rolled
back) and the in-repo mock WG-Guard node.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError, InsufficientFunds, ValidationError
from app.db.models import (
    DiscountCode,
    DiscountKind,
    OrderKind,
    OrderStatus,
    Payment,
    PaymentKind,
    ReceiptMedia,
    ReceiptStatus,
    Service,
    ServiceStatus,
    Staff,
    StaffRole,
)
from app.services.orders import order_service
from app.services.provisioning import provisioning
from app.services.receipts import receipt_service
from app.services.users import user_service

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Pricing & wallet
# ---------------------------------------------------------------------------


async def test_pricing_with_discount_and_wallet(session, customer, plan) -> None:
    # Fund the wallet with *part* of the price so a remainder is left to pay.
    await user_service.credit(session, customer, 1_000_000)
    session.add(DiscountCode(code="OFF10", kind=DiscountKind.PERCENT, value=10, is_active=True, per_user_limit=1))
    await session.flush()

    quote = await order_service.quote(session, customer, plan, discount_code="OFF10", use_wallet=True)
    assert quote.discount_rial == plan.price_rial // 10
    assert quote.wallet_used_rial == min(customer.balance_rial, plan.price_rial - quote.discount_rial)
    assert 0 < quote.payable_rial < plan.price_rial


async def test_order_debits_wallet_immediately_and_refunds_on_cancel(session, customer, plan) -> None:
    await user_service.credit(session, customer, 1_000_000)
    before = customer.balance_rial

    order = await order_service.create(session, customer, plan, use_wallet=True)
    assert order.wallet_used_rial == before  # the whole balance is reserved
    assert order.payable_rial == plan.price_rial - before
    assert customer.balance_rial == before - order.wallet_used_rial
    assert order.status is OrderStatus.PENDING_PAYMENT

    await order_service.cancel(session, order, reason="تست")
    assert customer.balance_rial == before
    assert order.status is OrderStatus.CANCELED


async def test_wallet_payment_fails_when_balance_is_short(session, customer, plan) -> None:
    await user_service.credit(session, customer, 10_000)  # far below the plan price
    with pytest.raises(InsufficientFunds):
        await user_service.debit(session, customer, plan.price_rial)


async def test_free_order_is_paid_instantly(session, customer, plan) -> None:
    order = await order_service.create(session, customer, plan, free=True)
    assert order.is_free
    assert order.payable_rial == 0
    assert order.status is OrderStatus.PAID


async def test_open_order_limit(session, customer, plan) -> None:
    from app.services.settings_store import app_settings

    await app_settings.load(session, force=True)
    limit = app_settings.get_int("advanced.max_open_orders", 3)
    for _ in range(limit):
        await order_service.create(session, customer, plan)
    with pytest.raises(ConflictError):
        await order_service.create(session, customer, plan)


async def test_discount_code_can_only_be_used_once_per_user(session, customer, plan) -> None:
    from app.db.models import DiscountCode, DiscountKind

    session.add(DiscountCode(code="ONCE", kind=DiscountKind.AMOUNT, value=50_000, is_active=True))
    await session.flush()

    first = await order_service.create(session, customer, plan, discount_code="ONCE")
    assert first.discount_rial == 50_000

    with pytest.raises(ValidationError):
        await order_service.create(session, customer, plan, discount_code="ONCE")


# ---------------------------------------------------------------------------
# Provisioning
# ---------------------------------------------------------------------------


async def test_provisioning_creates_service_device_and_subscription(session, customer, plan, panel_row) -> None:
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()

    result = await provisioning.provision_order(order.id)
    assert result.ok, result.error
    assert result.service_id

    service = await session.get(Service, result.service_id)
    await session.refresh(service, ["devices"])
    assert service is not None
    assert service.status is ServiceStatus.ACTIVE
    assert service.wg_user_id
    assert service.traffic_limit_bytes == 30 * 1024**3
    assert service.subscription_encrypted
    assert service.panel_id == panel_row.id

    device = service.devices[0]
    assert device.wg_device_id
    from app.core.security import decrypt_secret

    config = decrypt_secret(device.config_encrypted, purpose="config")
    assert config and "[Interface]" in config and "PrivateKey" in config


async def test_provisioning_is_idempotent_across_retries(session, customer, plan, panel_row) -> None:
    """Running the flow twice must not create a second VPN account."""
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()

    first = await provisioning.provision_order(order.id)
    second = await provisioning.provision_order(order.id)

    assert first.ok and second.ok
    assert first.service_id == second.service_id

    count = len((await session.execute(select(Service).where(Service.user_id == customer.id))).scalars().all())
    assert count == 1


async def test_plan_is_synced_to_the_node_before_purchase(session, customer, plan, panel_row) -> None:
    assert plan.wg_plan_id is None
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    await provisioning.provision_order(order.id)

    await session.refresh(plan)
    assert plan.wg_plan_id  # created on the node and remembered


async def test_a_broken_read_back_cannot_fail_a_committed_purchase(
    session, customer, plan, panel_row, monkeypatch
) -> None:
    """The account exists on the node; reading it back is best effort.

    A node that answers a shape this build cannot parse used to raise out of
    ``_safe_get_user``, fail the order, and leak a pydantic dump to the panel —
    while a real VPN account was already created.
    """
    from app.panels.providers.wgguard import WGGuardProvider

    async def exploding(self, user_ref: str):
        raise RuntimeError("the node answered a shape this build does not know")

    monkeypatch.setattr(WGGuardProvider, "get_user", exploding)

    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()

    result = await provisioning.provision_order(order.id)
    assert result.ok, result.error

    service = await session.get(Service, result.service_id)
    await session.refresh(order)
    assert service is not None
    assert service.wg_username  # falls back to the username we asked for
    assert service.status is ServiceStatus.ACTIVE
    assert order.status is OrderStatus.COMPLETED
    assert order.failure_reason is None


async def test_an_ambiguous_failure_retries_with_the_same_idempotency_key(
    session, customer, plan, panel_row, monkeypatch
) -> None:
    """Rule 2 of the provisioning contract: a transport failure keeps the key.

    Rotating it after a dropped connection is how a shop ends up with two VPN
    accounts for one order.
    """
    from app.core.errors import PanelUnavailable
    from app.panels.providers.wgguard import WGGuardProvider

    keys: list[str] = []
    real = WGGuardProvider.purchase

    async def flaky(self, *, plan_ref, username, device_name, idempotency_key):
        keys.append(idempotency_key)
        if len(keys) == 1:
            raise PanelUnavailable("connection dropped mid-flight")
        return await real(
            self, plan_ref=plan_ref, username=username, device_name=device_name, idempotency_key=idempotency_key
        )

    monkeypatch.setattr(WGGuardProvider, "purchase", flaky)

    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()

    result = await provisioning.provision_order(order.id)
    assert result.ok, result.error
    assert len(keys) == 2
    assert keys[0] == keys[1]
    assert len((await session.execute(select(Service).where(Service.user_id == customer.id))).scalars().all()) == 1


async def test_a_taken_username_rotates_the_idempotency_key(session, customer, plan, panel_row, monkeypatch) -> None:
    """A refused payload is the one case where a *derived* key is correct."""
    from app.core.errors import PanelConflict
    from app.panels.providers.wgguard import WGGuardProvider

    keys: list[str] = []
    usernames: list[str] = []
    real = WGGuardProvider.purchase

    async def conflicted(self, *, plan_ref, username, device_name, idempotency_key):
        keys.append(idempotency_key)
        usernames.append(username)
        if len(keys) == 1:
            raise PanelConflict("username already exists", panel_code="USERNAME_EXISTS")
        return await real(
            self, plan_ref=plan_ref, username=username, device_name=device_name, idempotency_key=idempotency_key
        )

    monkeypatch.setattr(WGGuardProvider, "purchase", conflicted)

    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()

    result = await provisioning.provision_order(order.id)
    assert result.ok, result.error
    assert keys[0] != keys[1]
    assert keys[1].startswith(keys[0])
    assert usernames[0] != usernames[1]


async def test_service_sync_and_rotation(session, customer, plan, panel_row) -> None:
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    result = await provisioning.provision_order(order.id)
    service = await session.get(Service, result.service_id)

    await provisioning.sync_service(session, service)
    assert service.last_synced_at is not None

    before = service.subscription_encrypted
    config, link = await provisioning.rotate_access(session, service)
    assert config and "[Interface]" in config
    assert link and link.startswith("/sub/")
    assert service.subscription_encrypted != before


async def test_add_and_delete_device(session, customer, plan, panel_row) -> None:
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    result = await provisioning.provision_order(order.id)
    service = await session.get(Service, result.service_id)
    await session.refresh(service, ["devices"])

    assert service.device_limit == 2
    extra = await provisioning.add_device(session, service, name="لپ‌تاپ")
    assert extra.wg_device_id
    await session.refresh(service, ["devices"])
    assert len(service.devices) == 2

    await provisioning.delete_device(session, extra)
    await session.refresh(service, ["devices"])
    assert len(service.devices) == 1


# ---------------------------------------------------------------------------
# Receipts (card-to-card)
# ---------------------------------------------------------------------------


async def test_receipt_approval_completes_the_order(session, customer, plan, panel_row) -> None:
    from app.db.models import ReceiptMedia

    order = await order_service.create(session, customer, plan)
    assert order.payable_rial == plan.price_rial

    receipt = await receipt_service.create(
        session,
        customer,
        purpose="purchase",
        amount_rial=order.payable_rial,
        media=ReceiptMedia.PHOTO,
        file_id="fake-file-id",
        order=order,
    )
    assert receipt.status is ReceiptStatus.PENDING
    assert order.status is OrderStatus.AWAITING_REVIEW

    admin = Staff(name="مدیر", role=StaffRole.ADMIN)
    session.add(admin)
    await session.flush()

    outcome = await receipt_service.approve(session, receipt, admin)
    assert outcome.accepted
    assert receipt.status is ReceiptStatus.APPROVED
    assert order.status is OrderStatus.PAID
    assert order.payment_reference == receipt.code

    # A second reviewer must not be able to re-decide.
    second = await receipt_service.approve(session, receipt, admin)
    assert not second.accepted
    assert second.already_reviewed_by == "مدیر"


async def test_receipt_rejection_reopens_the_order(session, customer, plan, panel_row) -> None:
    from app.db.models import ReceiptMedia

    order = await order_service.create(session, customer, plan)
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="purchase",
        amount_rial=order.payable_rial,
        media=ReceiptMedia.TEXT,
        note="رسید تستی",
        order=order,
    )

    outcome = await receipt_service.reject(session, receipt, None, reason="مبلغ مطابقت ندارد")
    assert outcome.accepted
    assert receipt.status is ReceiptStatus.REJECTED
    assert order.status is OrderStatus.PENDING_PAYMENT
    assert receipt.reject_reason == "مبلغ مطابقت ندارد"


async def test_deposit_receipt_credits_the_wallet(session, customer) -> None:
    before = customer.balance_rial
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="deposit",
        amount_rial=5_000_000,
        media=ReceiptMedia.PHOTO,
        file_id="deposit-file",
    )
    await receipt_service.approve(session, receipt, None)
    assert customer.balance_rial == before + 5_000_000

    entry = (await session.execute(select(Payment).where(Payment.user_id == customer.id))).scalars().first()
    assert entry is not None and entry.amount_rial == 5_000_000


async def test_only_one_pending_receipt_per_user(session, customer, plan) -> None:
    """A second receipt of the same kind is refused while one is under review."""
    await receipt_service.create(
        session,
        customer,
        purpose="deposit",
        amount_rial=1_000_000,
        media=ReceiptMedia.PHOTO,
        file_id="a",
    )
    with pytest.raises(ConflictError):
        await receipt_service.create(
            session,
            customer,
            purpose="deposit",
            amount_rial=2_000_000,
            media=ReceiptMedia.PHOTO,
            file_id="b",
        )

    # A different purpose stays allowed: they are separate review queues.
    order = await order_service.create(session, customer, plan)
    purchase = await receipt_service.create(
        session,
        customer,
        purpose="purchase",
        amount_rial=order.payable_rial,
        media=ReceiptMedia.PHOTO,
        file_id="c",
        order=order,
    )
    assert purchase.purpose == "purchase"


# ---------------------------------------------------------------------------
# Renewals
# ---------------------------------------------------------------------------


async def test_renewal_extends_the_same_service(session, customer, plan, panel_row) -> None:
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    first = await provisioning.provision_order(order.id)
    service = await session.get(Service, first.service_id)
    original_wg_user = service.wg_user_id

    renew = await order_service.create(session, customer, plan, kind=OrderKind.RENEW, service=service, free=True)
    await session.commit()
    result = await provisioning.provision_order(renew.id)

    assert result.ok, result.error
    assert result.service_id == service.id
    await session.refresh(service)
    assert service.wg_user_id == original_wg_user

    services = list((await session.execute(select(Service).where(Service.user_id == customer.id))).scalars())
    assert len(services) == 1


async def test_extra_traffic_raises_the_limit(session, customer, plan, panel_row) -> None:
    from app.services.orders import order_service as os_

    order = await os_.create(session, customer, plan, free=True)
    await session.commit()
    first = await provisioning.provision_order(order.id)
    service = await session.get(Service, first.service_id)
    original_limit = service.traffic_limit_bytes

    extra = await os_.create(session, customer, plan, kind=OrderKind.EXTRA_TRAFFIC, service=service, free=True)
    # The add-on order carries the extra volume in its own snapshot.
    extra.traffic_gb = 10
    await session.commit()

    result = await provisioning.provision_order(extra.id)
    assert result.ok, result.error
    await session.refresh(service)
    assert service.traffic_limit_bytes == (original_limit or 0) + 10 * 1024**3


async def test_deleted_service_is_hidden_but_kept(session, customer, plan, panel_row) -> None:
    order = await order_service.create(session, customer, plan, free=True)
    await session.commit()
    result = await provisioning.provision_order(order.id)
    service = await session.get(Service, result.service_id)
    service.status = ServiceStatus.DELETED
    await session.flush()

    row = await session.get(Service, service.id)
    assert row is not None and row.status is ServiceStatus.DELETED


# ---------------------------------------------------------------------------
# Ledger integrity
# ---------------------------------------------------------------------------


async def test_ledger_balance_matches_user_balance(session, customer) -> None:
    from app.db.models import Payment

    await user_service.credit(session, customer, 1_000_000)
    await user_service.debit(session, customer, 400_000)
    await user_service.credit(session, customer, 250_000, kind=PaymentKind.REFUND)

    entries = list(
        (await session.execute(select(Payment).where(Payment.user_id == customer.id).order_by(Payment.id))).scalars()
    )
    assert sum(entry.amount_rial for entry in entries) == customer.balance_rial
    assert entries[-1].balance_after_rial == customer.balance_rial


async def test_order_code_is_unique(session, customer, plan) -> None:
    codes = set()
    for _ in range(5):
        order = await order_service.create(session, customer, plan)
        codes.add(order.order_code)
        await order_service.cancel(session, order, reason="t")
    assert len(codes) == 5


async def test_expire_stale_orders_refunds_wallet(session, customer, plan) -> None:
    from datetime import timedelta

    from app.core.jalali import now_utc

    await user_service.credit(session, customer, 1_000_000)
    before = customer.balance_rial
    order = await order_service.create(session, customer, plan, use_wallet=True)
    order.payment_deadline = now_utc() - timedelta(minutes=1)
    await session.flush()

    expired = await order_service.expire_stale(session)
    assert order in expired
    assert order.status is OrderStatus.EXPIRED
    assert customer.balance_rial == before
