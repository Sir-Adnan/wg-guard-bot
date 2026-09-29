"""Category tree, gift codes and guides — the newer shop features."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.db.models import GiftCode, Plan, PlanCategory, User
from app.services.categories import MAX_DEPTH, categories
from app.services.gifts import gifts
from app.services.guides import guides

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Category tree
# ---------------------------------------------------------------------------


async def test_nested_categories(session) -> None:
    root = await categories.create(session, name="سرویس‌ها", icon="service")
    child = await categories.create(session, name="اقتصادی", parent_id=root.id)
    grandchild = await categories.create(session, name="یک ماهه", parent_id=child.id)

    assert await categories.depth_of(session, grandchild) == 3
    trail = await categories.breadcrumb(session, grandchild.id)
    assert [node.name for node in trail] == ["سرویس‌ها", "اقتصادی", "یک ماهه"]

    roots = await categories.roots(session)
    assert [node.name for node in roots] == ["سرویس‌ها"]

    children = await categories.children(session, root.id)
    assert [node.name for node in children] == ["اقتصادی"]


async def test_sibling_names_must_be_unique(session) -> None:
    await categories.create(session, name="تکراری")
    with pytest.raises(ConflictError):
        await categories.create(session, name="تکراری")
    # The same name under a different parent is fine.
    parent = await categories.create(session, name="والد")
    await categories.create(session, name="تکراری", parent_id=parent.id)


async def test_category_cannot_be_its_own_ancestor(session) -> None:
    root = await categories.create(session, name="ریشه")
    child = await categories.create(session, name="فرزند", parent_id=root.id)

    with pytest.raises(ValidationError):
        await categories.update(session, root.id, parent_id=child.id)
    with pytest.raises(ValidationError):
        await categories.update(session, root.id, parent_id=root.id)


async def test_max_depth_is_enforced(session) -> None:
    parent_id = None
    for level in range(MAX_DEPTH):
        node = await categories.create(session, name=f"سطح {level}", parent_id=parent_id)
        parent_id = node.id
    with pytest.raises(ValidationError):
        await categories.create(session, name="عمیق‌تر", parent_id=parent_id)


async def test_deleting_a_category_reparents_its_plans(session, plan) -> None:
    root = await categories.create(session, name="ریشه")
    child = await categories.create(session, name="فرزند", parent_id=root.id)
    plan.category_id = child.id
    await session.flush()

    moved = await categories.delete(session, root.id)
    assert moved == 1

    await session.refresh(plan)
    assert plan.category_id is None  # moved to "بدون دسته", never deleted
    assert await session.get(Plan, plan.id) is not None


async def test_tree_reports_plan_counts(session, plan) -> None:
    root = await categories.create(session, name="ریشه")
    child = await categories.create(session, name="فرزند", parent_id=root.id)
    plan.category_id = child.id
    await session.flush()

    tree = await categories.tree(session)
    assert len(tree) == 1
    assert tree[0].plan_count == 0
    assert tree[0].children[0].plan_count == 1
    assert tree[0].total_plans == 1


async def test_flatten_is_depth_ordered(session) -> None:
    root = await categories.create(session, name="A", sort_order=1)
    await categories.create(session, name="B", sort_order=2)
    await categories.create(session, name="A1", parent_id=root.id)

    flat = await categories.flatten(session)
    assert [(depth, node.name) for depth, node in flat] == [(0, "A"), (1, "A1"), (0, "B")]


async def test_move_swaps_order(session) -> None:
    await categories.create(session, name="اول", sort_order=1)
    second = await categories.create(session, name="دوم", sort_order=2)

    await categories.move(session, second.id, -1)
    children = await categories.children(session, None, active_only=False)
    assert children[0].name == "دوم"


# ---------------------------------------------------------------------------
# Gift codes
# ---------------------------------------------------------------------------


async def test_wallet_gift_credits_the_wallet(session, customer) -> None:
    code = await gifts.create(session, code="GIFT100", kind="wallet", value=1_000_000)
    before = customer.balance_rial

    result = await gifts.redeem(session, "gift100", customer)
    assert result.amount_rial == 1_000_000
    assert customer.balance_rial == before + 1_000_000
    assert code.used_count == 1


async def test_gift_code_respects_per_user_limit(session, customer) -> None:
    await gifts.create(session, code="ONCE", kind="wallet", value=1_000, per_user_limit=1)
    await gifts.redeem(session, "ONCE", customer)
    with pytest.raises(ValidationError):
        await gifts.redeem(session, "ONCE", customer)


async def test_gift_code_respects_max_uses(session, customer) -> None:
    await gifts.create(session, code="LIMITED", kind="wallet", value=1_000, max_uses=1, per_user_limit=5)
    await gifts.redeem(session, "LIMITED", customer)

    other = User(telegram_id=999_888_777, first_name="دیگری", referral_code="wgother")
    session.add(other)
    await session.flush()
    with pytest.raises(ValidationError):
        await gifts.redeem(session, "LIMITED", other)


async def test_expired_gift_code_is_refused(session, customer) -> None:
    from datetime import timedelta

    from app.core.jalali import now_utc

    await gifts.create(session, code="OLD", kind="wallet", value=1_000)
    row = await gifts.find(session, "OLD")
    row.expires_at = now_utc() - timedelta(days=1)
    await session.flush()

    with pytest.raises(ValidationError):
        await gifts.redeem(session, "OLD", customer)


async def test_plan_gift_creates_a_free_order(session, customer, plan) -> None:
    await gifts.create(session, code="FREEPLAN", kind="plan", value=plan.id)
    result = await gifts.redeem(session, "FREEPLAN", customer)

    assert result.plan is not None and result.plan.id == plan.id
    order = await gifts.create_free_order(session, result, customer)
    assert order.is_free and order.payable_rial == 0
    assert result.order_id == order.id


async def test_percent_gift_applies_to_a_top_up(session, customer) -> None:
    await gifts.create(session, code="BONUS20", kind="percent", value=20)
    before = customer.balance_rial
    result = await gifts.redeem(session, "BONUS20", customer, topup_rial=1_000_000)
    assert result.amount_rial == 200_000
    assert customer.balance_rial == before + 200_000


async def test_percent_gift_needs_a_top_up(session, customer) -> None:
    await gifts.create(session, code="NEEDTOP", kind="percent", value=10)
    with pytest.raises(ValidationError):
        await gifts.redeem(session, "NEEDTOP", customer)


async def test_bulk_generation_creates_unique_codes(session) -> None:
    created = await gifts.bulk_create(session, count=5, kind="wallet", value=50_000)
    codes = {row.code for row in created}
    assert len(codes) == 5
    stored = list((await session.execute(select(GiftCode))).scalars())
    assert len(stored) == 5


async def test_unknown_gift_code(session, customer) -> None:
    with pytest.raises(ValidationError):
        await gifts.redeem(session, "NOPE", customer)


async def test_gift_admin_toggle_and_delete(session) -> None:
    row = await gifts.create(session, code="ADMIN", kind="wallet", value=1_000)
    await gifts.toggle(session, row.id, False)
    assert row.is_active is False
    await gifts.delete(session, row.id)
    with pytest.raises(NotFoundError):
        await gifts.toggle(session, row.id, True)


# ---------------------------------------------------------------------------
# Guides
# ---------------------------------------------------------------------------


async def test_guide_crud_and_sections(session) -> None:
    android = await guides.create(
        session,
        title="نصب روی اندروید",
        section="connect",
        platform="android",
        body="<b>مرحله ۱</b> برنامه را نصب کنید.",
    )
    await guides.create(session, title="سؤال پرتکرار", section="faq", platform="all")

    section = await guides.list_section(session, "connect", platform="android")
    assert [guide.title for guide in section] == ["نصب روی اندروید"]
    # "all" guides are included for a specific platform too.
    assert len(await guides.list_section(session, "connect", platform="ios")) == 0

    counts = await guides.sections(session)
    assert counts["connect"] == 1 and counts["faq"] == 1

    await guides.bump_views(session, android.id)
    assert android.views == 1

    await guides.update(session, android.id, title="نصب روی اندروید و iOS")
    assert (await guides.get(session, android.id)).title == "نصب روی اندروید و iOS"

    await guides.delete(session, android.id)
    with pytest.raises(NotFoundError):
        await guides.get(session, android.id)


async def test_guide_rejects_bad_section_and_platform(session) -> None:
    with pytest.raises(ValidationError):
        await guides.create(session, title="x", section="nope")
    with pytest.raises(ValidationError):
        await guides.create(session, title="x", section="faq", platform="nope")
    with pytest.raises(ValidationError):
        await guides.create(session, title="   ")


# ---------------------------------------------------------------------------
# Catalog helpers used by the category navigation
# ---------------------------------------------------------------------------


async def test_catalog_filters_by_category_subtree(session, plan) -> None:
    from app.services.catalog import catalog

    root = await categories.create(session, name="ریشه")
    child = await categories.create(session, name="فرزند", parent_id=root.id)
    plan.category_id = child.id
    await session.flush()

    assert await catalog.list_plans(session, category_id=root.id) == []
    found = await catalog.list_plans(session, category_id=root.id, include_descendants=True)
    assert [item.id for item in found] == [plan.id]


async def test_featured_plans_only(session, plan) -> None:
    from app.services.catalog import catalog

    assert await catalog.list_plans(session, featured_only=True) == []
    plan.is_featured = True
    await session.flush()
    assert [item.id for item in await catalog.list_plans(session, featured_only=True)] == [plan.id]


async def test_test_plans_are_hidden_from_the_shop(session, plan) -> None:
    from app.services.catalog import catalog

    trial = Plan(name="سرویس تست", price_rial=0, is_test=True, is_active=True)
    session.add(trial)
    await session.flush()

    shop = await catalog.list_plans(session)
    assert trial.id not in [item.id for item in shop]
    assert [item.id for item in await catalog.test_plans(session)] == [trial.id]


async def test_stock_is_decremented_on_sale(session, plan) -> None:
    from app.services.catalog import catalog

    plan.is_unlimited_stock = False
    plan.stock = 2
    await session.flush()

    assert catalog.is_available(plan)
    await catalog.decrement_stock(session, plan)
    await catalog.decrement_stock(session, plan)
    assert plan.stock == 0
    assert not catalog.is_available(plan)


async def test_plan_category_relationship_backfills(session) -> None:
    """A category deletion must not orphan the relationship object."""
    root = await categories.create(session, name="ریشه")
    plan = Plan(name="پلن", price_rial=1000, is_active=True, category_id=root.id)
    session.add(plan)
    await session.flush()
    assert plan.category_node is not None
    assert isinstance(plan.category_node, PlanCategory)
