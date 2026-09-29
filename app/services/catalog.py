"""Plan catalog queries.

The catalog is intentionally thin: pricing lives on the row, terms are snapshotted
onto the order at purchase time (so editing a plan never rewrites history), and
the WG-Guard node copy is created on demand by
:meth:`app.panels.manager.PanelManager.ensure_node_plan`.
"""

from __future__ import annotations

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models import Order, OrderStatus, Panel, Plan, Service, ServiceStatus

log = get_logger(__name__)

SORT_MODES = {
    "price_asc": (Plan.price_rial.asc(),),
    "price_desc": (Plan.price_rial.desc(),),
    "traffic_asc": (Plan.traffic_gb.asc().nulls_last(),),
    "traffic_desc": (Plan.traffic_gb.desc().nulls_last(),),
    "default": (Plan.sort_order.asc(), Plan.price_rial.asc()),
}


class CatalogService:
    """Read/write helpers around :class:`~app.db.models.Plan`."""

    # -- customer-facing ---------------------------------------------------
    async def list_plans(
        self,
        session: AsyncSession,
        *,
        include_test: bool = False,
        include_inactive: bool = False,
        category: str | None = None,
        category_id: int | None = None,
        include_descendants: bool = False,
        featured_only: bool = False,
        sort: str = "default",
    ) -> list[Plan]:
        stmt = select(Plan)
        if not include_inactive:
            stmt = stmt.where(Plan.is_active.is_(True))
        if not include_test:
            stmt = stmt.where(Plan.is_test.is_(False))
        if category:
            stmt = stmt.where(Plan.category == category)
        if category_id is not None:
            ids = [category_id]
            if include_descendants:
                ids = await category_subtree_ids(session, category_id)
            stmt = stmt.where(Plan.category_id.in_(ids))
        if featured_only:
            stmt = stmt.where(Plan.is_featured.is_(True))
        stmt = stmt.order_by(*SORT_MODES.get(sort, SORT_MODES["default"]))
        return list((await session.execute(stmt)).scalars())

    async def test_plans(self, session: AsyncSession) -> list[Plan]:
        return list(
            (
                await session.execute(
                    select(Plan)
                    .where(Plan.is_active.is_(True), Plan.is_test.is_(True))
                    .order_by(Plan.sort_order.asc(), Plan.price_rial.asc())
                )
            ).scalars()
        )

    async def get(self, session: AsyncSession, plan_id: int, *, require_active: bool = False) -> Plan:
        plan = await session.get(Plan, plan_id)
        if plan is None:
            raise NotFoundError("پلن مورد نظر پیدا نشد.")
        if require_active and not plan.is_active:
            raise ValidationError("این پلن در حال حاضر قابل خریداری نیست.")
        return plan

    async def categories(self, session: AsyncSession) -> list[str]:
        rows = await session.execute(
            select(Plan.category)
            .where(Plan.is_active.is_(True), Plan.is_test.is_(False), Plan.category.is_not(None))
            .distinct()
            .order_by(Plan.category)
        )
        return [row for row in rows.scalars() if row]

    # -- stock -------------------------------------------------------------
    @staticmethod
    def is_available(plan: Plan) -> bool:
        if not plan.is_active:
            return False
        if plan.is_unlimited_stock:
            return True
        return bool(plan.stock and plan.stock > 0)

    @staticmethod
    def remaining_stock(plan: Plan) -> int | None:
        return None if plan.is_unlimited_stock else max(int(plan.stock or 0), 0)

    async def decrement_stock(self, session: AsyncSession, plan: Plan) -> None:
        if not plan.is_unlimited_stock and plan.stock is not None:
            plan.stock = max(int(plan.stock) - 1, 0)
            await session.flush()

    # -- admin -------------------------------------------------------------
    async def count_visible_plans(self, session: AsyncSession) -> int:
        """How many plans a customer can actually buy right now."""
        return int(
            await session.scalar(select(func.count(Plan.id)).where(Plan.is_active.is_(True), Plan.is_test.is_(False)))
            or 0
        )

    async def search(
        self,
        session: AsyncSession,
        query: str = "",
        *,
        only_active: bool | None = None,
        category_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Plan], int]:
        stmt = select(Plan)
        count_stmt = select(func.count(Plan.id))
        if query:
            condition = or_(Plan.name.ilike(f"%{query}%"), Plan.description.ilike(f"%{query}%"))
            stmt = stmt.where(condition)
            count_stmt = count_stmt.where(condition)
        if only_active is not None:
            stmt = stmt.where(Plan.is_active.is_(only_active))
            count_stmt = count_stmt.where(Plan.is_active.is_(only_active))
        if category_id is not None:
            stmt = stmt.where(Plan.category_id == category_id)
            count_stmt = count_stmt.where(Plan.category_id == category_id)

        total = int(await session.scalar(count_stmt) or 0)
        rows = list(
            (
                await session.execute(stmt.order_by(Plan.sort_order.asc(), Plan.id.asc()).limit(limit).offset(offset))
            ).scalars()
        )
        return rows, total

    async def stats(self, session: AsyncSession, plan_id: int) -> dict[str, int]:
        """Sales counters for one plan (uses the order snapshot, not live joins)."""
        sold = (
            await session.scalar(
                select(func.count(Order.id)).where(
                    Order.plan_id == plan_id,
                    Order.status.in_((OrderStatus.PAID, OrderStatus.PROVISIONING, OrderStatus.COMPLETED)),
                )
            )
            or 0
        )
        active = (
            await session.scalar(
                select(func.count(Service.id)).where(Service.plan_id == plan_id, Service.status == ServiceStatus.ACTIVE)
            )
            or 0
        )
        return {"sold": int(sold), "active_services": int(active)}

    async def panels_for_select(self, session: AsyncSession) -> list[Panel]:
        return list(
            (
                await session.execute(
                    select(Panel).where(Panel.is_active.is_(True)).order_by(Panel.sort_order.asc(), Panel.id.asc())
                )
            ).scalars()
        )


catalog = CatalogService()


async def category_subtree_ids(session: AsyncSession, root_id: int) -> list[int]:
    """``root_id`` plus every descendant category id (breadth-first, cycle-safe)."""
    from app.db.models import PlanCategory

    ids: list[int] = []
    queue = [root_id]
    seen: set[int] = set()
    while queue:
        current = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        ids.append(current)
        children = (await session.execute(select(PlanCategory.id).where(PlanCategory.parent_id == current))).scalars()
        queue.extend(int(child) for child in children)
    return ids


__all__ = ["SORT_MODES", "CatalogService", "catalog", "category_subtree_ids"]
