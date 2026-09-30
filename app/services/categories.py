"""Category tree for the shop.

Depth is unbounded: a category may contain sub-categories, which may contain
sub-sub-categories, and plans hang off any node.  The bot navigates the tree one
level at a time; the panel renders it as an indented list.

Keeping this in its own module means the tree rules (cycle prevention, sibling
uniqueness, cascade delete) live in exactly one place.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import Plan, PlanCategory
from app.services.audit import audit

log = get_logger(__name__)

MAX_DEPTH = 5

#: ``audit_logs`` action written when a category's plan selection changes.
AUDIT_ACTION = "category.plans"

#: ``audit_logs.entity`` for those entries.
AUDIT_ENTITY = "plan_category"


@dataclass(slots=True)
class CategoryNode:
    """A category plus its resolved children — used by the panel's tree view."""

    category: PlanCategory
    children: list[CategoryNode] = field(default_factory=list)
    plan_count: int = 0

    @property
    def total_plans(self) -> int:
        return self.plan_count + sum(child.total_plans for child in self.children)


class CategoryService:
    """CRUD and traversal for :class:`~app.db.models.PlanCategory`."""

    # -- reads -------------------------------------------------------------
    async def get(self, session: AsyncSession, category_id: int, *, require_visible: bool = False) -> PlanCategory:
        """Load one category.

        ``require_visible`` is what the bot passes: a stale or hand-made callback
        payload must not open a category the owner hid or switched off.  The
        panel deliberately reads hidden rows too, so it is opt-in.
        """
        node = await session.get(PlanCategory, category_id)
        if node is None:
            raise NotFoundError("دسته‌بندی مورد نظر پیدا نشد.")
        if require_visible and not (node.is_active and node.is_visible):
            raise NotFoundError("این دسته‌بندی در دسترس نیست.")
        return node

    async def all(self, session: AsyncSession, *, active_only: bool = False) -> list[PlanCategory]:
        stmt = select(PlanCategory)
        if active_only:
            stmt = stmt.where(PlanCategory.is_active.is_(True))
        stmt = stmt.order_by(PlanCategory.sort_order.asc(), PlanCategory.name.asc())
        return list((await session.execute(stmt)).scalars())

    async def children(
        self, session: AsyncSession, parent_id: int | None, *, active_only: bool = True
    ) -> list[PlanCategory]:
        stmt = select(PlanCategory)
        if parent_id is None:
            stmt = stmt.where(PlanCategory.parent_id.is_(None))
        else:
            stmt = stmt.where(PlanCategory.parent_id == parent_id)
        if active_only:
            stmt = stmt.where(PlanCategory.is_active.is_(True), PlanCategory.is_visible.is_(True))
        stmt = stmt.order_by(PlanCategory.sort_order.asc(), PlanCategory.id.asc())
        return list((await session.execute(stmt)).scalars())

    async def roots(self, session: AsyncSession, *, active_only: bool = True) -> list[PlanCategory]:
        return await self.children(session, None, active_only=active_only)

    async def breadcrumb(self, session: AsyncSession, category_id: int | None) -> list[PlanCategory]:
        """Path from the root down to ``category_id`` (cycle-safe)."""
        trail: list[PlanCategory] = []
        seen: set[int] = set()
        node = await session.get(PlanCategory, category_id) if category_id else None
        while node is not None and node.id not in seen:
            seen.add(node.id)
            trail.append(node)
            node = node.parent
        return list(reversed(trail))

    async def plan_counts(self, session: AsyncSession, *, active_only: bool = True) -> dict[int, int]:
        stmt = select(Plan.category_id, func.count(Plan.id)).group_by(Plan.category_id)
        if active_only:
            stmt = stmt.where(Plan.is_active.is_(True))
        return {
            int(category_id): int(count)
            for category_id, count in (await session.execute(stmt)).all()
            if category_id is not None
        }

    async def tree(self, session: AsyncSession) -> list[CategoryNode]:
        """Full tree with per-node plan counts (panel view)."""
        categories = await self.all(session)
        counts = await self.plan_counts(session, active_only=False)

        nodes: dict[int, CategoryNode] = {
            category.id: CategoryNode(category=category, plan_count=counts.get(category.id, 0))
            for category in categories
        }
        roots: list[CategoryNode] = []
        for category in categories:
            node = nodes[category.id]
            parent = nodes.get(category.parent_id) if category.parent_id else None
            (parent.children if parent else roots).append(node)
        return roots

    async def plan_options(self, session: AsyncSession) -> list[tuple[Plan, str | None]]:
        """Every plan with the name of the category it sits in right now.

        The panel's plan picker needs one row per plan — including the inactive
        and test ones, which never reach the shop — so the operator can see what
        a tick would move.  The name is joined explicitly instead of read through
        ``plan.category_node`` so a plan whose category vanished still appears,
        as «بدون دسته».
        """
        stmt = (
            select(Plan, PlanCategory.name)
            .outerjoin(PlanCategory, Plan.category_id == PlanCategory.id)
            .order_by(Plan.sort_order.asc(), Plan.id.asc())
        )
        return [(plan, name) for plan, name in (await session.execute(stmt)).all()]

    async def flatten(self, session: AsyncSession, *, active_only: bool = True) -> list[tuple[int, PlanCategory]]:
        """``[(depth, category)]`` in display order — handy for ``<select>`` boxes."""
        out: list[tuple[int, PlanCategory]] = []

        async def walk(parent_id: int | None, depth: int) -> None:
            for child in await self.children(session, parent_id, active_only=active_only):
                out.append((depth, child))
                await walk(child.id, depth + 1)

        await walk(None, 0)
        return out

    # -- writes ------------------------------------------------------------
    async def depth_of(self, session: AsyncSession, node: PlanCategory) -> int:
        """Depth of ``node`` (1 = top level).

        Walks the chain with explicit ``session.get`` calls: a lazy
        ``node.parent.parent`` chain would raise ``MissingGreenlet`` under
        asyncio, which is exactly the kind of bug that only shows up in
        production.
        """
        depth = 1
        seen = {node.id}
        cursor = node.parent_id
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            parent = await session.get(PlanCategory, cursor)
            if parent is None:
                break
            depth += 1
            cursor = parent.parent_id
        return depth

    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        parent_id: int | None = None,
        description: str | None = None,
        icon: str | None = None,
        sort_order: int = 0,
        is_active: bool = True,
        is_visible: bool = True,
    ) -> PlanCategory:
        clean = name.strip()
        if not clean:
            raise ValidationError("نام دسته‌بندی نمی‌تواند خالی باشد.")

        if parent_id is not None:
            parent = await session.get(PlanCategory, parent_id)
            if parent is None:
                raise NotFoundError("دسته‌بندی والد پیدا نشد.")
            if await self.depth_of(session, parent) >= MAX_DEPTH:
                raise ValidationError(f"حداکثر عمق دسته‌بندی‌ها {MAX_DEPTH} سطح است.")

        await self._assert_unique_name(session, parent_id, clean)

        node = PlanCategory(
            parent_id=parent_id,
            name=clean[:128],
            description=description,
            icon=icon,
            sort_order=sort_order,
            is_active=is_active,
            is_visible=is_visible,
        )
        session.add(node)
        await session.flush()
        log.info("Category created: %s (parent=%s)", node.name, parent_id)
        return node

    async def update(
        self,
        session: AsyncSession,
        category_id: int,
        *,
        name: str | None = None,
        parent_id: int | None = None,
        description: str | None = None,
        icon: str | None = None,
        sort_order: int | None = None,
        is_active: bool | None = None,
        is_visible: bool | None = None,
    ) -> PlanCategory:
        node = await self.get(session, category_id)

        if name is not None and name.strip() and name.strip() != node.name:
            await self._assert_unique_name(session, node.parent_id, name.strip(), exclude_id=node.id)
            node.name = name.strip()[:128]

        if parent_id is not None and parent_id != node.parent_id:
            await self._assert_no_cycle(session, node, parent_id)
            node.parent_id = parent_id

        if description is not None:
            node.description = description
        if icon is not None:
            node.icon = icon or None
        if sort_order is not None:
            node.sort_order = sort_order
        if is_active is not None:
            node.is_active = is_active
        if is_visible is not None:
            node.is_visible = is_visible

        await session.flush()
        return node

    async def delete(self, session: AsyncSession, category_id: int, *, reassign_to: int | None = None) -> int:
        """Delete a category and its whole subtree in one statement.

        Plans inside the deleted subtree are re-parented to ``reassign_to``
        (``None`` = uncategorised) instead of being deleted — losing a catalog
        entry because someone reorganised the tree would be unforgivable.

        The subtree is removed with a bulk ``DELETE`` rather than ORM cascades:
        cascade traversal loads every descendant one by one and blows up with
        ``MissingGreenlet`` under asyncio.
        """
        node = await self.get(session, category_id)
        subtree_ids = await self._subtree_ids(session, node)
        moved = await self._reparent_plans(session, subtree_ids, reassign_to)

        await session.execute(delete(PlanCategory).where(PlanCategory.id.in_(subtree_ids)))
        await session.flush()
        log.info("Category %s deleted (%d nodes, %d plans moved)", node.name, len(subtree_ids), moved)
        return moved

    async def move(self, session: AsyncSession, category_id: int, direction: int) -> PlanCategory:
        """Swap ``sort_order`` with the previous/next sibling."""
        node = await self.get(session, category_id)
        siblings = await self.children(session, node.parent_id, active_only=False)
        index = next((i for i, s in enumerate(siblings) if s.id == node.id), None)
        if index is None:
            return node
        target_index = index + direction
        if not 0 <= target_index < len(siblings):
            return node
        other = siblings[target_index]
        node.sort_order, other.sort_order = other.sort_order, node.sort_order
        if node.sort_order == other.sort_order:
            # Both were 0 — fall back to positional values.
            node.sort_order, other.sort_order = target_index, index
        await session.flush()
        return node

    async def set_plans(self, session: AsyncSession, category_id: int, plan_ids: Sequence[int]) -> int:
        """Make ``plan_ids`` exactly the plans of one category.

        The panel's picker submits its **whole** selection, so a plan that is in
        the category today and is missing from ``plan_ids`` is detached
        (``category_id`` becomes ``None`` = «بدون دسته»); an empty selection
        therefore clears the category.  Plans in other categories are never
        touched, so one click can only ever change the membership of this one
        node — the tree keeps its "a plan belongs to at most one category" rule.

        Returns how many rows changed, and ``0`` for a submission that matches
        what is already stored (which writes no audit entry either).  An unknown
        plan id raises :class:`~app.core.errors.ValidationError` before anything
        is written.  The caller owns the transaction: ``await session.commit()``
        afterwards, or roll back and nothing happened.
        """
        node = await self.get(session, category_id)
        wanted = list(dict.fromkeys(plan_ids))

        if wanted:
            known = set((await session.execute(select(Plan.id).where(Plan.id.in_(wanted)))).scalars())
            if len(known) != len(wanted):
                raise ValidationError("یکی از پلن‌های انتخابی پیدا نشد؛ صفحه را دوباره باز کنید.")

        chosen = set(wanted)
        attached = detached = 0

        current = list((await session.execute(select(Plan).where(Plan.category_id == category_id))).scalars())
        for plan in current:
            if plan.id not in chosen:
                plan.category_id = None
                detached += 1

        if wanted:
            rows = list((await session.execute(select(Plan).where(Plan.id.in_(wanted)))).scalars())
            for plan in rows:
                if plan.category_id != category_id:
                    plan.category_id = category_id
                    attached += 1

        changed = attached + detached
        if not changed:
            return 0

        await session.flush()
        description = f"پلن‌های «{node.name}»: {fa_digits(attached)} وصل و {fa_digits(detached)} جدا شد."
        await audit.record(
            session,
            AUDIT_ACTION,
            entity=AUDIT_ENTITY,
            entity_id=category_id,
            description=description,
            meta={"category_id": category_id, "attached": attached, "detached": detached, "plans": sorted(chosen)},
        )
        log.info("Category %s plans set: %d attached, %d detached", node.name, attached, detached)
        return changed

    # -- helpers -----------------------------------------------------------
    async def _assert_unique_name(
        self, session: AsyncSession, parent_id: int | None, name: str, *, exclude_id: int | None = None
    ) -> None:
        stmt = select(func.count(PlanCategory.id)).where(PlanCategory.name == name)
        stmt = stmt.where(
            PlanCategory.parent_id.is_(None) if parent_id is None else PlanCategory.parent_id == parent_id
        )
        if exclude_id is not None:
            stmt = stmt.where(PlanCategory.id != exclude_id)
        if await session.scalar(stmt):
            raise ConflictError("دسته‌بندی با این نام در همین سطح وجود دارد.")

    async def _assert_no_cycle(self, session: AsyncSession, node: PlanCategory, new_parent_id: int | None) -> None:
        """Refuse to move a node underneath one of its own descendants."""
        if new_parent_id is None:
            return
        if new_parent_id == node.id:
            raise ValidationError("یک دسته‌بندی نمی‌تواند والد خودش باشد.")
        parent = await session.get(PlanCategory, new_parent_id)
        if parent is None:
            raise NotFoundError("دسته‌بندی والد پیدا نشد.")

        # Walk up from the prospective parent: hitting ``node`` means a cycle.
        depth = await self.depth_of(session, parent)
        cursor: PlanCategory | None = parent
        seen: set[int] = set()
        while cursor is not None and cursor.id not in seen:
            seen.add(cursor.id)
            if cursor.id == node.id:
                raise ValidationError("انتقال این دسته‌بندی باعث ایجاد حلقه می‌شود.")
            cursor = await session.get(PlanCategory, cursor.parent_id) if cursor.parent_id else None

        if depth + self._subtree_height(node) > MAX_DEPTH:
            raise ValidationError(f"حداکثر عمق دسته‌بندی‌ها {MAX_DEPTH} سطح است.")

    def _subtree_height(self, node: PlanCategory) -> int:
        if not node.children:
            return 1
        return 1 + max(self._subtree_height(child) for child in node.children)

    async def _subtree_ids(self, session: AsyncSession, node: PlanCategory) -> list[int]:
        ids = [node.id]
        queue = [node.id]
        while queue:
            current = queue.pop()
            for child in await self.children(session, current, active_only=False):
                ids.append(child.id)
                queue.append(child.id)
        return ids

    async def _reparent_plans(self, session: AsyncSession, category_ids: list[int], target: int | None) -> int:
        plans = list((await session.execute(select(Plan).where(Plan.category_id.in_(category_ids)))).scalars())
        for plan in plans:
            plan.category_id = target
        if plans:
            await session.flush()
        return len(plans)


categories = CategoryService()


__all__ = ["AUDIT_ACTION", "AUDIT_ENTITY", "MAX_DEPTH", "CategoryNode", "CategoryService", "categories"]
