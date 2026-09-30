"""Choosing a category's plans — the picker on the categories page.

``CategoryService.set_plans`` is the write path: the picker posts its whole
selection, so a plan that is left unticked is detached and plans of other
categories are never touched.  ``POST {panel_prefix}/categories/{id}/plans`` is
the panel's door to it, and the plans page's category filter is the other half
of the same question — which plans live where — so it is covered here too.
"""

from __future__ import annotations

import re

import httpx
import pytest
from sqlalchemy import select

from app.core.errors import NotFoundError, ValidationError
from app.db.models import AuditLog, Plan
from app.services.categories import AUDIT_ACTION, categories

pytestmark = pytest.mark.db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _plan(session, name: str, *, category_id: int | None = None) -> Plan:
    row = Plan(
        name=name,
        price_rial=2_500_000,
        traffic_gb=30,
        duration_days=30,
        category_id=category_id,
        is_active=True,
        is_unlimited_stock=True,
        username_template="wg{tg}",
    )
    session.add(row)
    await session.flush()
    return row


async def _audit_entries(session) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.action == AUDIT_ACTION)
    return list((await session.execute(stmt)).scalars())


def _csrf(html: str) -> str | None:
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = html.find('"', start)
    return html[start:end] if end > start else None


def _picker_html(html: str, category_id: int) -> str:
    """The rendered markup of one category's plan picker."""
    start = html.find(f'id="category-plans-{category_id}"')
    assert start != -1, f"no picker rendered for category {category_id}"
    end = html.find('id="category-plans-', start + 1)
    return html[start : end if end != -1 else len(html)]


def _checkbox(chunk: str, plan_id: int) -> str:
    """The checkbox tag of one plan inside a rendered picker."""
    match = re.search(rf'<input[^>]*name="plan_ids"[^>]*value="{plan_id}"[^>]*>', chunk)
    assert match, f"no plan_ids checkbox for plan {plan_id}"
    return match.group(0)


def _tree_row(html: str, category_id: int) -> str:
    """The rendered markup of one row of the category tree (plus its subtree)."""
    start = html.find(f'data-drag-id="{category_id}"')
    assert start != -1, f"category {category_id} is not in the tree"
    end = html.find('data-drag-id="', start + 1)
    return html[start : end if end != -1 else len(html)]


# ---------------------------------------------------------------------------
# CategoryService.set_plans
# ---------------------------------------------------------------------------
async def test_set_plans_assigns_the_selection_and_detaches_the_rest(session) -> None:
    root = await categories.create(session, name="آلمان")
    other = await categories.create(session, name="فرانسه")
    staying = await _plan(session, "یک ماهه", category_id=root.id)
    newcomer = await _plan(session, "سه ماهه")
    outsider = await _plan(session, "پلن فرانسه", category_id=other.id)

    assert await categories.set_plans(session, root.id, [staying.id, newcomer.id]) == 1
    assert staying.category_id == root.id
    assert newcomer.category_id == root.id
    assert outsider.category_id == other.id  # never asked about, never touched

    # The picker posts the whole selection: dropping a plan detaches it again.
    leaving = await _plan(session, "شش ماهه", category_id=root.id)
    assert await categories.set_plans(session, root.id, [staying.id, newcomer.id]) == 1
    assert leaving.category_id is None
    assert outsider.category_id == other.id


async def test_an_empty_selection_clears_the_category(session) -> None:
    root = await categories.create(session, name="آلمان")
    plan = await _plan(session, "یک ماهه", category_id=root.id)

    assert await categories.set_plans(session, root.id, []) == 1
    assert plan.category_id is None


async def test_a_repeated_submission_changes_nothing(session) -> None:
    root = await categories.create(session, name="آلمان")
    plan = await _plan(session, "یک ماهه", category_id=root.id)

    assert await categories.set_plans(session, root.id, [plan.id, plan.id]) == 0
    assert plan.category_id == root.id
    assert await _audit_entries(session) == []


async def test_an_unknown_plan_is_refused_without_writing_anything(session) -> None:
    root = await categories.create(session, name="آلمان")
    staying = await _plan(session, "یک ماهه", category_id=root.id)
    newcomer = await _plan(session, "سه ماهه")

    with pytest.raises(ValidationError):
        await categories.set_plans(session, root.id, [newcomer.id, 987_654])

    assert staying.category_id == root.id
    assert newcomer.category_id is None
    assert await _audit_entries(session) == []


async def test_an_unknown_category_is_refused(session) -> None:
    plan = await _plan(session, "یک ماهه")

    with pytest.raises(NotFoundError):
        await categories.set_plans(session, 987_654, [plan.id])
    assert plan.category_id is None


async def test_a_change_writes_exactly_one_audit_entry(session) -> None:
    root = await categories.create(session, name="آلمان")
    staying = await _plan(session, "یک ماهه", category_id=root.id)
    newcomer = await _plan(session, "سه ماهه")

    await categories.set_plans(session, root.id, [newcomer.id])

    entries = await _audit_entries(session)
    assert len(entries) == 1
    assert entries[0].entity == "plan_category"
    assert entries[0].entity_id == str(root.id)
    assert entries[0].meta["attached"] == 1
    assert entries[0].meta["detached"] == 1
    assert staying.category_id is None


async def test_plan_options_report_every_plan_and_where_it_lives(session) -> None:
    root = await categories.create(session, name="آلمان")
    inside = await _plan(session, "یک ماهه", category_id=root.id)
    outside = await _plan(session, "سه ماهه")

    options = await categories.plan_options(session)
    assert [(plan.id, name) for plan, name in options] == [(inside.id, "آلمان"), (outside.id, None)]


# ---------------------------------------------------------------------------
# The picker on the categories page
# ---------------------------------------------------------------------------
async def test_the_picker_pre_checks_the_plans_of_that_category(signed_in_client: httpx.AsyncClient, session) -> None:
    root = await categories.create(session, name="آلمان")
    other = await categories.create(session, name="فرانسه")
    inside = await _plan(session, "یک ماهه", category_id=root.id)
    elsewhere = await _plan(session, "سه ماهه", category_id=other.id)
    loose = await _plan(session, "شش ماهه")
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    assert page.status_code == 200

    picker = _picker_html(page.text, root.id)
    assert f'id="cat-plans-table-{root.id}"' in picker
    assert "checked" in _checkbox(picker, inside.id)
    assert "checked" not in _checkbox(picker, elsewhere.id)
    assert "checked" not in _checkbox(picker, loose.id)
    # Every row says where the plan lives now: here, another category, or nowhere.
    assert "همین دسته" in picker
    assert "فرانسه" in picker
    assert "بدون دسته" in picker
    # The row carries the button that opens this picker.
    assert f'data-modal-open="category-plans-{root.id}"' in page.text


async def test_the_panel_picker_assigns_and_detaches(signed_in_client: httpx.AsyncClient, session) -> None:
    root = await categories.create(session, name="آلمان")
    other = await categories.create(session, name="فرانسه")
    staying = await _plan(session, "یک ماهه", category_id=root.id)
    leaving = await _plan(session, "شش ماهه", category_id=root.id)
    newcomer = await _plan(session, "سه ماهه")
    outsider = await _plan(session, "پلن فرانسه", category_id=other.id)
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    token = _csrf(page.text)
    assert token

    response = await signed_in_client.post(
        f"/panel/categories/{root.id}/plans",
        data={"csrf_token": token, "plan_ids": [str(staying.id), str(newcomer.id)]},
    )
    assert response.status_code == 303

    for row in (staying, leaving, newcomer, outsider):
        await session.refresh(row)
    assert staying.category_id == root.id
    assert leaving.category_id is None  # unticked here, so detached here
    assert newcomer.category_id == root.id
    assert outsider.category_id == other.id

    follow_up = await signed_in_client.get("/panel/categories")
    assert "۱ پلن به دستهٔ «آلمان» وصل شد و ۱ پلن جدا شد." in follow_up.text


async def test_the_panel_refuses_an_unknown_plan_calmly(signed_in_client: httpx.AsyncClient, session) -> None:
    root = await categories.create(session, name="آلمان")
    inside = await _plan(session, "یک ماهه", category_id=root.id)
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    token = _csrf(page.text)

    response = await signed_in_client.post(
        f"/panel/categories/{root.id}/plans",
        data={"csrf_token": token, "plan_ids": [str(inside.id), "987654"]},
    )
    assert response.status_code == 303

    follow_up = await signed_in_client.get("/panel/categories")
    assert "پیدا نشد" in follow_up.text  # a calm sentence, not a stack trace

    await session.refresh(inside)
    assert inside.category_id == root.id


async def test_the_picker_needs_a_csrf_token(signed_in_client: httpx.AsyncClient, session) -> None:
    root = await categories.create(session, name="آلمان")
    await session.commit()

    response = await signed_in_client.post(f"/panel/categories/{root.id}/plans", data={"plan_ids": "1"})
    assert response.status_code == 403


async def test_saving_the_same_selection_says_nothing_changed(signed_in_client: httpx.AsyncClient, session) -> None:
    root = await categories.create(session, name="آلمان")
    inside = await _plan(session, "یک ماهه", category_id=root.id)
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    token = _csrf(page.text)

    response = await signed_in_client.post(
        f"/panel/categories/{root.id}/plans",
        data={"csrf_token": token, "plan_ids": str(inside.id)},
    )
    assert response.status_code == 303

    follow_up = await signed_in_client.get("/panel/categories")
    assert "بدون تغییر ماند" in follow_up.text
    await session.refresh(inside)
    assert inside.category_id == root.id


async def test_unticking_every_box_clears_the_category(signed_in_client: httpx.AsyncClient, session) -> None:
    """The picker posts its whole selection, so an empty one means "none"."""
    root = await categories.create(session, name="آلمان")
    inside = await _plan(session, "یک ماهه", category_id=root.id)
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    token = _csrf(page.text)

    response = await signed_in_client.post(f"/panel/categories/{root.id}/plans", data={"csrf_token": token})
    assert response.status_code == 303

    await session.refresh(inside)
    assert inside.category_id is None

    follow_up = await signed_in_client.get("/panel/categories")
    assert "۱ پلن از دستهٔ «آلمان» جدا شد" in follow_up.text


async def test_the_plan_modal_still_moves_one_plan(signed_in_client: httpx.AsyncClient, session) -> None:
    """The per-plan ``<select>`` stays the one-at-a-time path."""
    root = await categories.create(session, name="آلمان")
    plan = await _plan(session, "یک ماهه")
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _csrf(page.text)

    response = await signed_in_client.post(
        f"/panel/plans/{plan.id}",
        data={
            "csrf_token": token,
            "name": plan.name,
            "price": "250000",
            "category_id": str(root.id),
            "is_active": "1",
        },
    )
    assert response.status_code == 303

    await session.refresh(plan)
    assert plan.category_id == root.id


# ---------------------------------------------------------------------------
# Honest counts in the tree
# ---------------------------------------------------------------------------
async def test_the_tree_reports_the_total_and_names_the_subcategory_part(
    signed_in_client: httpx.AsyncClient, session
) -> None:
    root = await categories.create(session, name="آلمان")
    child = await categories.create(session, name="برلین", parent_id=root.id)
    await _plan(session, "پلن برلین", category_id=child.id)
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    assert page.status_code == 200

    # A branch that only holds sub-categories no longer reads «۰ پلن».
    root_row = _tree_row(page.text, root.id)
    assert "۱ پلن (۱ در زیردسته‌ها)" in root_row

    child_row = _tree_row(page.text, child.id)
    assert "۱ پلن" in child_row
    assert "در زیردسته‌ها" not in child_row


# ---------------------------------------------------------------------------
# The plans page's category filter
# ---------------------------------------------------------------------------
async def test_the_plans_page_filters_by_category(signed_in_client: httpx.AsyncClient, session) -> None:
    root = await categories.create(session, name="آلمان")
    child = await categories.create(session, name="برلین", parent_id=root.id)
    other = await categories.create(session, name="فرانسه")
    await _plan(session, "پلن برلین", category_id=child.id)
    await _plan(session, "پلن آلمان", category_id=root.id)
    await _plan(session, "پلن فرانسه", category_id=other.id)
    await _plan(session, "پلن بی‌دسته")
    await session.commit()

    unfiltered = await signed_in_client.get("/panel/plans")
    assert unfiltered.status_code == 200
    for name in ("پلن برلین", "پلن آلمان", "پلن فرانسه", "پلن بی‌دسته"):
        assert name in unfiltered.text
    assert "همه دسته‌ها</option>" in unfiltered.text
    assert "بدون دسته</option>" in unfiltered.text

    # A parent category includes its descendants.
    parent = await signed_in_client.get(f"/panel/plans?category_id={root.id}")
    assert parent.status_code == 200
    assert "پلن برلین" in parent.text and "پلن آلمان" in parent.text
    assert "پلن فرانسه" not in parent.text
    assert "پلن بی‌دسته" not in parent.text

    # «بدون دسته» is a filter of its own.
    loose = await signed_in_client.get("/panel/plans?category_id=none")
    assert loose.status_code == 200
    assert "پلن بی‌دسته" in loose.text
    assert "پلن آلمان" not in loose.text

    # A value that is neither an id nor «none» falls back to the whole catalogue.
    bogus = await signed_in_client.get("/panel/plans?category_id=برلین")
    assert bogus.status_code == 200
    assert "پلن فرانسه" in bogus.text


async def test_the_category_filter_survives_pagination(signed_in_client: httpx.AsyncClient, session) -> None:
    root = await categories.create(session, name="آلمان")
    for index in range(7):
        await _plan(session, f"پلن آلمان {index}", category_id=root.id)
    await _plan(session, "پلن بی‌دسته")
    await session.commit()

    page = await signed_in_client.get(f"/panel/plans?category_id={root.id}&size=5&page=2")
    assert page.status_code == 200
    assert f"category_id={root.id}" in page.text
    # Page two of the filtered list only holds plans of that category.
    assert "پلن آلمان 6" in page.text
    assert "پلن بی‌دسته" not in page.text
