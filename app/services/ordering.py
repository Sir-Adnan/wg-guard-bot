"""Generic display-order service for every panel list that can be reordered.

Seven tables in this repository carry a ``sort_order`` column and every one of
them means the same thing: "the operator's preferred position in a list".  The
panel renders those lists with drag handles, so the rules for turning a *list of
ids* into a dense, collision-free set of ``sort_order`` values have to live in
exactly one place — otherwise every page grows its own slightly different swap
logic and the audit trail fragments.

Design notes
------------
* **Dense, gapped numbering.**  A reordered list is renumbered
  ``(index + 1) * ORDER_STEP`` — gap 10 leaves room for a manual insert without
  touching its neighbours, and it is what :func:`next_sort_order` uses for the
  end of the list.
* **Scope is a partition, not a filter.**  ``plan_categories`` orders siblings
  inside one parent, so a scoped call only ever reads and writes the rows that
  share that parent.  Passing an id from another parent is an error, never a
  silent move between branches.
* **Partial lists are allowed and stable.**  The panel always posts the whole
  list, but a row created in another tab between render and drop must not lose
  its position: ids that are missing from the posted list keep their relative
  order after the ones that were placed.
* **A no-op is a no-op.**  When the posted order already matches the stored one,
  nothing is written and the caller gets ``0`` — so the audit log stays a record
  of changes rather than of clicks.

Layering: this module is a service, so it may import :mod:`app.core` and
:mod:`app.db` only (see ``AGENTS.md`` §1.3).  The web layer drives it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.core.errors import ValidationError
from app.core.money import fa_digits
from app.db.models import CardAccount, Channel, Guide, Panel, Plan, PlanCategory
from app.services.audit import audit

#: Gap between two neighbouring rows.  Ten keeps the numbers readable and leaves
#: room for a manual insert, and it is the step used when appending at the end.
ORDER_STEP = 10

#: Audit action prefix: one entry per entity, e.g. ``order.plans``.
AUDIT_ACTION = "order"

#: The table an audit entry about ``plans`` writes to (``audit_logs.entity``).
AUDIT_ENTITY = "ordering"


@dataclass(frozen=True, slots=True)
class OrderSpec:
    """Everything the generic reordering rules need to know about one entity.

    ``scope_column`` is the optional partition key — the column a list is
    ordered *within*.  Categories use ``parent_id`` (siblings inside one parent);
    every other entity has a single flat list and leaves it ``None``.
    """

    key: str
    model: type[Any]
    order_column: str
    label: str
    scope_column: str | None = None

    @property
    def scope_column_attr(self) -> InstrumentedAttribute | None:
        return getattr(self.model, self.scope_column) if self.scope_column else None

    @property
    def supports_scope(self) -> bool:
        """True when the entity's lists are partitioned by ``scope_column``."""
        return self.scope_column is not None


# ---------------------------------------------------------------------------
# Registry — add one entry and an entity is reorderable
# ---------------------------------------------------------------------------
ORDERABLE: dict[str, OrderSpec] = {
    "plans": OrderSpec(key="plans", model=Plan, order_column="sort_order", label="پلن\u200cها"),
    "categories": OrderSpec(
        key="categories",
        model=PlanCategory,
        order_column="sort_order",
        label="دسته\u200cبندی\u200cها",
        scope_column="parent_id",
    ),
    "channels": OrderSpec(key="channels", model=Channel, order_column="sort_order", label="کانال\u200cها"),
    "cards": OrderSpec(key="cards", model=CardAccount, order_column="sort_order", label="کارت\u200cهای بانکی"),
    "guides": OrderSpec(key="guides", model=Guide, order_column="sort_order", label="آموزش\u200cها"),
    "panels": OrderSpec(key="panels", model=Panel, order_column="sort_order", label="پنل\u200cهای VPN"),
}


def spec_for(entity: str) -> OrderSpec:
    """Look up a registry entry, refusing anything the panel cannot reorder.

    The message is shown to an operator, so it names the list rather than the
    internal key and never leaks the request payload.
    """
    spec = ORDERABLE.get((entity or "").strip().lower())
    if spec is None:
        raise ValidationError("این فهرست از تغییر ترتیب پشتیبانی نمی\u200cکند.")
    return spec


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
def _scope_of(spec: OrderSpec, scope: int | None) -> int | None:
    """Validate the scope argument against what the entity supports."""
    if scope is None:
        return None
    if not spec.supports_scope:
        raise ValidationError(f"فهرست «{spec.label}» بخش\u200cبندی نمی\u200cشود.")
    if scope < 1:
        raise ValidationError("بخش انتخابی معتبر نیست.")
    return scope


def _order_clause(spec: OrderSpec):
    column = getattr(spec.model, spec.order_column)
    return (column.asc(), spec.model.id.asc())


def _scope_clause(spec: OrderSpec, scope: int | None):
    """``WHERE`` fragment selecting exactly the rows of one scoped list."""
    column = spec.scope_column_attr
    if column is None:
        return None
    return column.is_(None) if scope is None else column == scope


async def _scoped_rows(session: AsyncSession, spec: OrderSpec, scope: int | None) -> list[Any]:
    """Every row of one list, in the order the panel shows them."""
    stmt = select(spec.model)
    clause = _scope_clause(spec, scope)
    if clause is not None:
        stmt = stmt.where(clause)
    return list((await session.execute(stmt.order_by(*_order_clause(spec)))).scalars())


async def next_sort_order(session: AsyncSession, entity: str, *, scope: int | None = None) -> int:
    """Value a *new* row should get so it lands at the end of its list.

    Every create route calls this now that the numeric field is gone from the
    forms: a new row must append, never jump to the top of a curated order.
    """
    spec = spec_for(entity)
    scope = _scope_of(spec, scope)
    column = getattr(spec.model, spec.order_column)
    stmt = select(func.max(column))
    clause = _scope_clause(spec, scope)
    if clause is not None:
        stmt = stmt.where(clause)
    highest = await session.scalar(stmt)
    return int(highest or 0) + ORDER_STEP


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
def _validate_ids(spec: OrderSpec, ids: Sequence[int], known: dict[int, Any]) -> list[int]:
    """Refuse an impossible order before a single row is touched.

    Raises :class:`~app.core.errors.ValidationError` for a duplicated id or an
    id that is not part of this list (unknown, deleted, or belonging to another
    parent) — the panel then flashes a calm sentence instead of half-applying a
    reorder that would have moved a row across branches.
    """
    if not ids:
        raise ValidationError(f"ترتیبی برای «{spec.label}» فرستاده نشد.")

    seen: set[int] = set()
    for item_id in ids:
        if item_id in seen:
            raise ValidationError("یک مورد دو بار در ترتیب جدید آمده است.")
        seen.add(item_id)
        if item_id not in known:
            raise ValidationError(f"یکی از موارد «{spec.label}» پیدا نشد؛ صفحه را دوباره باز کنید.")
    return list(ids)


def _placed(ids: Sequence[int], ordered_ids: Sequence[int]) -> list[int]:
    """Full id list in the new order: posted ids first, the rest in place.

    ``ordered_ids`` is the stored order of the whole list.  Anything the caller
    did not mention keeps its previous relative position after the placed rows,
    so a concurrent insert survives a drag.
    """
    placed = list(ids)
    chosen = set(placed)
    placed.extend(item_id for item_id in ordered_ids if item_id not in chosen)
    return placed


def _target_orders(placed: Sequence[int]) -> dict[int, int]:
    return {item_id: (index + 1) * ORDER_STEP for index, item_id in enumerate(placed)}


def _audit_description(spec: OrderSpec, changed: int) -> str:
    return f"{spec.label}: ترتیب {fa_digits(changed)} مورد بازچینی شد."


async def apply_order(session: AsyncSession, entity: str, ids: Sequence[int], *, scope: int | None = None) -> int:
    """Renumber one list into ``ids`` order; returns how many rows changed.

    ``ids`` is the whole list as the operator wants it (the panel posts every
    visible id).  Values become ``(index + 1) * ORDER_STEP``, so the result is
    dense, gap-tolerant and independent of wherever the rows started.

    * a no-op order writes nothing and returns ``0``;
    * an unknown or out-of-scope id raises
      :class:`~app.core.errors.ValidationError` and leaves the table untouched;
    * exactly one audit entry is written when something changed.

    The caller owns the transaction: ``await session.commit()`` afterwards, or
    roll back and the reorder never happened.
    """
    spec = spec_for(entity)
    scope = _scope_of(spec, scope)

    rows = await _scoped_rows(session, spec, scope)
    known = {row.id: row for row in rows}
    ids = _validate_ids(spec, ids, known)

    placed = _placed(ids, [row.id for row in rows])
    targets = _target_orders(placed)

    touched: list[int] = []
    for row in rows:
        target = targets[row.id]
        if int(getattr(row, spec.order_column)) != target:
            setattr(row, spec.order_column, target)
            touched.append(row.id)

    if not touched:
        return 0

    await session.flush()
    await audit.record(
        session,
        f"{AUDIT_ACTION}.{spec.key}",
        entity=AUDIT_ENTITY,
        entity_id=",".join(str(item_id) for item_id in touched),
        description=_audit_description(spec, len(touched)),
        meta={"scope": scope, "changed": len(touched)},
    )
    return len(touched)


async def move(session: AsyncSession, entity: str, item_id: int, direction: int, *, scope: int | None = None) -> int:
    """Swap one row with its previous/next sibling; returns rows changed.

    This is the arrow-button path, and it is deliberately built on the same
    rules as :func:`apply_order` so a list can never end up in a state a drag
    could not have produced.  ``direction`` is ``-1`` for "one step up the
    list" and ``1`` for "one step down"; at either end the call is a no-op and
    returns ``0``.
    """
    if direction not in (-1, 1):
        raise ValidationError("جهت جابه\u200cجایی نامعتبر است.")

    spec = spec_for(entity)
    scope = _scope_of(spec, scope)

    rows = await _scoped_rows(session, spec, scope)
    ordered_ids = [row.id for row in rows]
    index = next((position for position, row in enumerate(rows) if row.id == item_id), None)
    if index is None:
        raise ValidationError(f"این مورد در «{spec.label}» پیدا نشد.")

    target = index + direction
    if not 0 <= target < len(ordered_ids):
        return 0

    ordered_ids[index], ordered_ids[target] = ordered_ids[target], ordered_ids[index]
    return await apply_order(session, entity, ordered_ids, scope=scope)


__all__ = [
    "AUDIT_ACTION",
    "AUDIT_ENTITY",
    "ORDERABLE",
    "ORDER_STEP",
    "OrderSpec",
    "apply_order",
    "move",
    "next_sort_order",
    "spec_for",
]
