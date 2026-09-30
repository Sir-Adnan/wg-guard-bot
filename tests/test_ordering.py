"""Drag-and-drop ordering: the service, the one endpoint and the page hooks.

The feature is deliberately generic — one registry entry, one ``POST
{panel_prefix}/reorder``, one JavaScript behaviour — so these tests drive the
*service* rules through the real panel app instead of through six copies of the
same page.  That is the blast radius that matters: a mistake here reorders a
list the operator curated by hand.

Covered:

* dense renumbering, and a no-op that reports ``changed == 0``;
* an id outside the list (unknown, or another parent's child) is refused and
  nothing moves;
* a scoped reorder never touches another parent's rows;
* exactly one audit entry per change;
* ``next_sort_order`` appends, which is what the create routes now use;
* the endpoint's two response shapes (JSON for ``fetch``, 303 + flash for a
  form post) and its CSRF check;
* every page renders the drag hooks the JavaScript opts into.
"""

from __future__ import annotations

import httpx
import pytest
from conftest import PANEL_PASSWORD

pytestmark = pytest.mark.db

#: ``(path, entity)`` for the six lists that opted into reordering.
DRAG_PAGES: tuple[tuple[str, str], ...] = (
    ("/panel/plans", "plans"),
    ("/panel/categories", "categories"),
    ("/panel/channels", "channels"),
    ("/panel/cards", "cards"),
    ("/panel/guides", "guides"),
    ("/panel/panels", "panels"),
)

REORDER_URL = "/panel/reorder"


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------
async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post("/panel/login", data={"login": "owner", "password": PANEL_PASSWORD})
    assert response.status_code == 303, response.text


def _extract_csrf(html: str) -> str:
    """Scrape the hidden input the way a browser would submit it."""
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    assert start != -1, "the page carries no csrf_token input"
    start += len(marker)
    end = html.find('"', start)
    assert end > start
    return html[start:end]


def _drag_list_entity(html: str) -> str:
    """The entity of the page's outermost draggable list."""
    marker = 'data-drag-list="'
    start = html.find(marker)
    assert start != -1, "no [data-drag-list] on the page"
    start += len(marker)
    return html[start : html.find('"', start)]


def _drag_item_ids(html: str) -> list[int]:
    """Every ``data-drag-id`` in document order — the order the panel shows."""
    out: list[int] = []
    marker = 'data-drag-id="'
    start = html.find(marker)
    while start != -1:
        start += len(marker)
        end = html.find('"', start)
        out.append(int(html[start:end]))
        start = html.find(marker, end)
    return out


def _drag_scope(html: str) -> str | None:
    marker = 'data-drag-scope="'
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    return html[start : html.find('"', start)]


def _row_ids(session_rows) -> list[int]:
    return [row.id for row in session_rows]


async def _plan_rows(session, **where) -> list:
    """The plans list in exactly the order the page shows it."""
    from sqlalchemy import select

    from app.db.models import Plan

    stmt = select(Plan)
    for key, value in where.items():
        stmt = stmt.where(getattr(Plan, key) == value)
    return list((await session.execute(stmt.order_by(Plan.sort_order.asc(), Plan.id.asc()))).scalars())


async def _make_plan(session, name: str, sort_order: int):
    from app.db.models import Plan

    row = Plan(name=name, price_rial=1_000_000, sort_order=sort_order, username_template="wg{tg}")
    session.add(row)
    await session.flush()
    return row


async def _make_node(session, name: str, *, parent_id: int | None = None, sort_order: int = 0):
    from app.db.models import PlanCategory

    row = PlanCategory(name=name, parent_id=parent_id, sort_order=sort_order)
    session.add(row)
    await session.flush()
    return row


async def _make_card(session, holder: str, sort_order: int, number: str):
    from app.db.models import CardAccount

    row = CardAccount(card_number=number, holder_name=holder, sort_order=sort_order)
    session.add(row)
    await session.flush()
    return row


async def _make_channel(session, chat_id: str, sort_order: int):
    from app.db.models import Channel

    row = Channel(chat_id=chat_id, title=chat_id, sort_order=sort_order)
    session.add(row)
    await session.flush()
    return row


async def _make_guide(session, title: str, sort_order: int):
    from app.db.models import Guide

    row = Guide(title=title, body="متن", sort_order=sort_order)
    session.add(row)
    await session.flush()
    return row


async def _make_panel(session, name: str, sort_order: int):
    from app.db.models import Panel

    row = Panel(
        name=name,
        base_url="http://panel.test",
        api_token_encrypted="x",
        sort_order=sort_order,
    )
    session.add(row)
    await session.flush()
    return row


async def _audit_rows(session) -> list:
    from sqlalchemy import select

    from app.db.models import AuditLog

    return list((await session.execute(select(AuditLog).order_by(AuditLog.id.asc()))).scalars())


# ---------------------------------------------------------------------------
# The service: apply_order
# ---------------------------------------------------------------------------
async def test_apply_order_renumbers_densely(session) -> None:
    from app.services.ordering import ORDER_STEP, apply_order

    rows = [await _make_plan(session, f"plan-{index}", 0) for index in range(3)]
    ids = _row_ids(rows)

    changed = await apply_order(session, "plans", list(reversed(ids)))
    await session.commit()

    ordered = await _plan_rows(session)
    assert [row.id for row in ordered] == list(reversed(ids))
    assert [row.sort_order for row in ordered] == [ORDER_STEP, 2 * ORDER_STEP, 3 * ORDER_STEP]
    assert changed == 3


async def test_apply_order_is_a_no_op_when_the_order_already_matches(session) -> None:
    from app.services.ordering import apply_order

    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(3)]

    changed = await apply_order(session, "plans", _row_ids(rows))
    await session.commit()

    assert changed == 0
    assert await _audit_rows(session) == []


async def test_apply_order_refuses_an_unknown_id_and_writes_nothing(session) -> None:
    from app.core.errors import ValidationError
    from app.services.ordering import apply_order

    first = await _make_plan(session, "first", 10)
    second = await _make_plan(session, "second", 20)

    with pytest.raises(ValidationError):
        await apply_order(session, "plans", [second.id, 999_999, first.id])
    await session.commit()

    ordered = await _plan_rows(session)
    assert [row.id for row in ordered] == [first.id, second.id]
    assert [row.sort_order for row in ordered] == [10, 20]


async def test_apply_order_refuses_a_missing_list(session) -> None:
    from app.core.errors import ValidationError
    from app.services.ordering import apply_order

    with pytest.raises(ValidationError):
        await apply_order(session, "plans", [])


async def test_apply_order_only_moves_rows_the_operator_placed(session) -> None:
    """A row created in another tab keeps its position at the end."""
    from app.services.ordering import apply_order

    first = await _make_plan(session, "first", 10)
    await _make_plan(session, "second", 20)
    await _make_plan(session, "third", 30)
    last = await _make_plan(session, "fourth", 40)

    await apply_order(session, "plans", [last.id, first.id])
    await session.commit()

    ordered = await _plan_rows(session)
    assert ordered[0].id == last.id
    assert ordered[1].id == first.id
    # untouched rows keep their earlier order, after the placed ones
    assert [row.name for row in ordered[2:]] == ["second", "third"]


@pytest.mark.parametrize(
    ("entity", "maker"),
    [
        ("cards", _make_card),
        ("channels", _make_channel),
        ("guides", _make_guide),
        ("panels", _make_panel),
    ],
)
async def test_every_registered_list_can_be_reordered(session, entity: str, maker) -> None:
    """One registry entry is all a list needs — prove each one really works."""
    from sqlalchemy import select

    from app.services.ordering import apply_order, spec_for

    spec = spec_for(entity)
    if entity == "cards":
        rows = [await maker(session, f"holder-{index}", index * 10, f"603799111111110{index}") for index in range(3)]
    elif entity == "channels":
        rows = [await maker(session, f"@chan{index}", index * 10) for index in range(3)]
    elif entity == "guides":
        rows = [await maker(session, f"guide-{index}", index * 10) for index in range(3)]
    else:
        rows = [await maker(session, f"panel-{index}", index * 10) for index in range(3)]

    wanted = list(reversed(_row_ids(rows)))
    changed = await apply_order(session, entity, wanted)
    await session.commit()

    stored = list((await session.execute(select(spec.model).order_by(spec.model.sort_order.asc()))).scalars())
    assert [row.id for row in stored] == wanted
    # reversal moves every row that was not already in its new slot
    assert changed >= len(rows) - 1


async def test_unknown_entity_is_refused(session) -> None:
    from app.core.errors import ValidationError
    from app.services.ordering import apply_order

    with pytest.raises(ValidationError):
        await apply_order(session, "staff", [1])


# ---------------------------------------------------------------------------
# The service: scope
# ---------------------------------------------------------------------------
async def test_a_scoped_reorder_never_touches_another_parent(session) -> None:
    from app.services.ordering import apply_order

    root = await _make_node(session, "ریشه")
    other_parent = await _make_node(session, "والد دیگر")
    children = [await _make_node(session, f"فرزند {i}", parent_id=root.id, sort_order=(i + 1) * 10) for i in range(3)]
    stranger = await _make_node(session, "فرزند والد دیگر", parent_id=other_parent.id, sort_order=70)

    moved = await apply_order(session, "categories", list(reversed(_row_ids(children))), scope=root.id)
    await session.commit()

    # Two of the three rows change places; the middle one already sits at 20.
    assert moved == 2
    for row in (*children, stranger):
        await session.refresh(row)
    stored = [children[2], children[1], children[0]]
    assert [row.sort_order for row in stored] == [10, 20, 30]
    assert stranger.sort_order == 70


async def test_an_id_from_another_parent_is_refused_and_nothing_moves(session) -> None:
    from app.core.errors import ValidationError
    from app.services.ordering import apply_order

    root = await _make_node(session, "ریشه")
    other_parent = await _make_node(session, "والد دیگر")
    first = await _make_node(session, "فرزند ۱", parent_id=root.id, sort_order=10)
    second = await _make_node(session, "فرزند ۲", parent_id=root.id, sort_order=20)
    stranger = await _make_node(session, "غریبه", parent_id=other_parent.id, sort_order=30)

    with pytest.raises(ValidationError):
        await apply_order(session, "categories", [stranger.id, first.id, second.id], scope=root.id)
    await session.commit()

    assert (await session.get(type(first), first.id)).sort_order == 10
    assert (await session.get(type(second), second.id)).sort_order == 20
    assert (await session.get(type(stranger), stranger.id)).sort_order == 30


async def test_root_level_categories_are_their_own_scope(session) -> None:
    """``scope=None`` means "top level", not "everything"."""
    from app.services.ordering import apply_order

    first = await _make_node(session, "ریشه ۱", sort_order=10)
    second = await _make_node(session, "ریشه ۲", sort_order=20)
    child = await _make_node(session, "زیردسته", parent_id=first.id, sort_order=30)

    await apply_order(session, "categories", [second.id, first.id])
    await session.commit()

    assert (await session.get(type(first), first.id)).sort_order == 20
    assert (await session.get(type(second), second.id)).sort_order == 10
    assert (await session.get(type(child), child.id)).sort_order == 30


async def test_a_scope_is_refused_for_a_flat_list(session) -> None:
    from app.core.errors import ValidationError
    from app.services.ordering import apply_order

    row = await _make_plan(session, "plan", 10)
    with pytest.raises(ValidationError):
        await apply_order(session, "plans", [row.id], scope=1)


# ---------------------------------------------------------------------------
# The service: audit and next_sort_order
# ---------------------------------------------------------------------------
async def test_one_audit_entry_per_change(session) -> None:
    from app.services.ordering import apply_order

    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(3)]

    await apply_order(session, "plans", list(reversed(_row_ids(rows))))
    await apply_order(session, "plans", _row_ids(rows))
    await apply_order(session, "plans", _row_ids(rows))
    await session.commit()

    entries = await _audit_rows(session)
    assert len(entries) == 2
    assert entries[0].action == "order.plans"
    assert entries[0].entity == "ordering"
    # Every row whose position actually changed is listed, so the log says what
    # moved — the first reversal leaves the middle row where it was.
    assert set(entries[0].entity_id.split(",")) == {str(rows[0].id), str(rows[2].id)}


async def test_next_sort_order_appends_to_the_end(session) -> None:
    from app.services.ordering import ORDER_STEP, next_sort_order

    assert await next_sort_order(session, "plans") == ORDER_STEP

    await _make_plan(session, "only", 10)
    await _make_plan(session, "second", 60)
    assert await next_sort_order(session, "plans") == 70


async def test_next_sort_order_respects_the_scope(session) -> None:
    from app.services.ordering import next_sort_order

    root = await _make_node(session, "ریشه")
    other = await _make_node(session, "والد دیگر")
    await _make_node(session, "فرزند", parent_id=root.id, sort_order=10)

    assert await next_sort_order(session, "categories", scope=root.id) == 20
    assert await next_sort_order(session, "categories", scope=other.id) == 10
    assert await next_sort_order(session, "categories") == 10


# ---------------------------------------------------------------------------
# The service: move (the arrow buttons, and the keyboard path)
# ---------------------------------------------------------------------------
async def test_move_swaps_siblings_and_stops_at_the_edges(session) -> None:
    from app.services.ordering import move

    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(3)]

    assert await move(session, "plans", rows[2].id, -1) == 2
    await session.commit()
    ordered = await _plan_rows(session)
    assert [row.id for row in ordered] == [rows[0].id, rows[2].id, rows[1].id]
    assert [row.sort_order for row in ordered] == [10, 20, 30]

    assert await move(session, "plans", rows[0].id, -1) == 0  # already first
    assert await move(session, "plans", rows[1].id, 1) == 0  # already last


async def test_move_within_a_scope_leaves_other_branches_alone(session) -> None:
    from app.services.ordering import move

    root = await _make_node(session, "ریشه")
    other = await _make_node(session, "والد دیگر")
    children = [await _make_node(session, f"فرزند {i}", parent_id=root.id, sort_order=(i + 1) * 10) for i in range(3)]
    stranger = await _make_node(session, "غریبه", parent_id=other.id, sort_order=99)

    assert await move(session, "categories", children[0].id, 1, scope=root.id) == 2
    await session.commit()

    kind = type(root)
    assert (await session.get(kind, children[1].id)).sort_order == 10
    assert (await session.get(kind, children[0].id)).sort_order == 20
    assert (await session.get(kind, children[2].id)).sort_order == 30
    assert (await session.get(kind, stranger.id)).sort_order == 99


# ---------------------------------------------------------------------------
# The endpoint: JSON for a background call
# ---------------------------------------------------------------------------
async def test_fetch_request_gets_json(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={"entity": "plans", "ids": f"{rows[1].id} {rows[0].id}", "csrf_token": token},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"] is True
    assert payload["changed"] == 2
    assert "ترتیب" in payload["message"]

    ordered = await _plan_rows(session)
    assert [row.id for row in ordered] == [rows[1].id, rows[0].id]


async def test_json_is_also_served_for_an_accept_header(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={"entity": "plans", "ids": [str(rows[1].id), str(rows[0].id)], "csrf_token": token},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["changed"] == 2


async def test_fetch_no_op_reports_zero(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={"entity": "plans", "ids": [str(rows[0].id), str(rows[1].id)], "csrf_token": token},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "changed": 0,
        "message": "ترتیب «پلن\u200cها» از قبل همین بود؛ تغییری لازم نشد.",
    }


async def test_fetch_with_an_id_from_another_parent_is_refused(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    root = await _make_node(session, "ریشه")
    other = await _make_node(session, "والد دیگر")
    child = await _make_node(session, "فرزند", parent_id=root.id, sort_order=10)
    stranger = await _make_node(session, "غریبه", parent_id=other.id, sort_order=20)
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={
            "entity": "categories",
            "scope": str(root.id),
            "ids": [str(stranger.id), str(child.id)],
            "csrf_token": token,
        },
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 400, response.text
    payload = response.json()
    assert payload["ok"] is False
    assert payload["changed"] == 0
    assert "پیدا نشد" in payload["message"]
    # nothing moved
    assert (await session.get(type(child), child.id)).sort_order == 10
    assert (await session.get(type(stranger), stranger.id)).sort_order == 20


async def test_unknown_entity_is_refused_as_json(signed_in_client: httpx.AsyncClient, owner) -> None:
    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={"entity": "staff", "ids": "1", "csrf_token": token},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 400
    assert response.json()["ok"] is False


# ---------------------------------------------------------------------------
# The endpoint: form post (the no-JavaScript path)
# ---------------------------------------------------------------------------
async def test_form_post_redirects_with_a_flash(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={
            "entity": "plans",
            "ids": f"{rows[1].id},{rows[0].id}",
            "csrf_token": token,
            "next": "/panel/plans?page=1&size=20",
        },
        headers={"Referer": "http://panel.test/panel/plans"},
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == "/panel/plans?page=1&size=20"
    assert "wggb_flash" in response.cookies

    ordered = await _plan_rows(session)
    assert [row.id for row in ordered] == [rows[1].id, rows[0].id]

    follow_up = await signed_in_client.get("/panel/plans")
    assert "server-flash" in follow_up.text


async def test_form_post_falls_back_to_the_referer(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={"entity": "plans", "ids": [str(rows[1].id), str(rows[0].id)], "csrf_token": token},
        headers={"Referer": "http://panel.test/panel/plans?page=2"},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/panel/plans?page=2"


async def test_an_absolute_next_url_is_ignored(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    """An open redirect is not a feature: only a same-origin path is honoured."""
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    response = await signed_in_client.post(
        REORDER_URL,
        data={
            "entity": "plans",
            "ids": [str(rows[1].id), str(rows[0].id)],
            "csrf_token": token,
            "next": "https://evil.example/steal",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/panel/plans"


async def test_a_form_post_without_csrf_is_refused(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    response = await signed_in_client.post(REORDER_URL, data={"entity": "plans", "ids": [str(rows[1].id)]})
    assert response.status_code == 303
    assert response.headers["location"] == "/panel/"
    assert "wggb_flash" in response.cookies

    ordered = await _plan_rows(session)
    assert [row.id for row in ordered] == [rows[0].id, rows[1].id]


async def test_a_fetch_without_csrf_is_refused(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    rows = [await _make_plan(session, f"plan-{index}", (index + 1) * 10) for index in range(2)]
    await session.commit()

    response = await signed_in_client.post(
        REORDER_URL,
        data={"entity": "plans", "ids": [str(rows[1].id), str(rows[0].id)]},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 403
    payload = response.json()
    assert payload["ok"] is False
    assert payload["changed"] == 0
    assert "منقضی" in payload["message"]

    ordered = await _plan_rows(session)
    assert [row.id for row in ordered] == [rows[0].id, rows[1].id]


async def test_the_endpoint_needs_a_session(app_client: httpx.AsyncClient, owner) -> None:
    response = await app_client.post(REORDER_URL, data={"entity": "plans", "ids": "1"})
    assert response.status_code == 303
    assert "/panel/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# The pages: the drag hooks the JavaScript opts into
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(("path", "entity"), DRAG_PAGES)
async def test_every_page_renders_the_drag_hooks(
    signed_in_client: httpx.AsyncClient, owner, session, plan, panel_row, path: str, entity: str
) -> None:
    """A list is only draggable if the markup carries the attributes."""
    # one row in every list, so the hooks render even on an empty database
    root = await _make_node(session, "ریشه", sort_order=10)
    await _make_node(session, "زیردسته", parent_id=root.id, sort_order=10)
    await _make_card(session, "علی رضایی", 10, "6037991111111111")
    await _make_channel(session, "@wgguard", 10)
    await _make_guide(session, "آموزش اتصال", 10)
    await _make_panel(session, "سرور تست", 10)
    await session.commit()

    response = await signed_in_client.get(path)
    assert response.status_code == 200, response.text
    body = response.text

    assert _drag_list_entity(body) == entity
    assert "data-drag-handle" in body
    assert 'class="drag-handle"' in body
    assert "برای تغییر ترتیب، ردیف\u200cها را بکشید." in body
    assert _drag_item_ids(body), f"{path} rendered no draggable items"

    # the CSRF hook the background POST reads its token from
    assert 'data-csrf="' in body
    assert _extract_csrf(body)


async def test_plans_page_has_no_numeric_sort_field_and_keeps_priority(
    signed_in_client: httpx.AsyncClient, owner, session, plan, panel_row
) -> None:
    plans_page = await signed_in_client.get("/panel/plans")
    assert plans_page.status_code == 200
    assert 'name="sort_order"' not in plans_page.text

    panels_page = await signed_in_client.get("/panel/panels")
    assert panels_page.status_code == 200
    # ``priority`` is a selection knob, not a display order — it stays numeric
    assert 'name="priority"' in panels_page.text
    assert "عدد بزرگ\u200cتر زودتر انتخاب می\u200cشود." in panels_page.text
    assert 'name="sort_order"' not in panels_page.text


async def test_categories_page_drags_one_parent_at_a_time(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    """The tree nests one drag list per parent, each scoped to it."""
    first = await _make_node(session, "ریشه ۱", sort_order=10)
    second = await _make_node(session, "ریشه ۲", sort_order=20)
    await _make_node(session, "فرزند ۱", parent_id=first.id, sort_order=10)
    await _make_node(session, "فرزند ۲", parent_id=first.id, sort_order=20)
    await session.commit()

    response = await signed_in_client.get("/panel/categories")
    assert response.status_code == 200
    body = response.text

    assert body.count('data-drag-list="categories"') == 2  # roots + first's children
    assert _drag_scope(body) == str(first.id)
    # the root list carries no scope, so its ids are the top-level ones
    root_list = body.split('data-drag-list="categories"', 2)[1]
    assert str(first.id) in root_list
    assert str(second.id) in root_list


async def test_the_category_arrow_buttons_still_work(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    """The no-JavaScript fallback stays a first-class path."""
    root = await _make_node(session, "ریشه")
    rows = [await _make_node(session, f"فرزند {i}", parent_id=root.id, sort_order=(i + 1) * 10) for i in range(2)]
    await session.commit()

    page = await signed_in_client.get("/panel/categories")
    token = _extract_csrf(page.text)
    assert f'action="/panel/categories/{rows[1].id}/move"' in page.text

    response = await signed_in_client.post(
        f"/panel/categories/{rows[1].id}/move", data={"direction": "-1", "csrf_token": token}
    )
    assert response.status_code == 303

    # The route wrote through its own session, so re-read these two rows.
    await session.refresh(rows[1])
    await session.refresh(rows[0])
    assert rows[1].sort_order == 10
    assert rows[0].sort_order == 20


async def test_a_created_row_lands_at_the_end_of_the_list(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    """The create routes use ``next_sort_order`` now that the field is gone."""
    from app.services.ordering import ORDER_STEP

    await _make_card(session, "کارت اول", 10, "6037991111111111")
    await session.commit()

    page = await signed_in_client.get("/panel/cards")
    token = _extract_csrf(page.text)

    created = await signed_in_client.post(
        "/panel/cards",
        data={
            "csrf_token": token,
            "card_number": "6104337712349876",
            "holder_name": "کارت دوم",
            "is_active": "1",
        },
    )
    assert created.status_code == 303, created.text

    from sqlalchemy import select

    from app.db.models import CardAccount

    rows = list((await session.execute(select(CardAccount).order_by(CardAccount.sort_order.asc()))).scalars())
    assert [row.holder_name for row in rows] == ["کارت اول", "کارت دوم"]
    assert rows[-1].sort_order == 10 + ORDER_STEP


async def test_editing_a_row_does_not_reset_its_position(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    """A drag order must survive an edit through the modal."""
    plan = await _make_plan(session, "پلن اول", 10)
    other = await _make_plan(session, "پلن دوم", 20)
    await session.commit()

    page = await signed_in_client.get("/panel/plans")
    token = _extract_csrf(page.text)

    saved = await signed_in_client.post(
        f"/panel/plans/{other.id}",
        data={
            "csrf_token": token,
            "name": "پلن دوم (ویرایش‌شده)",
            "price": "250000",
            "device_limit": "1",
            "start_policy": "first_connection",
            "username_template": "wg{tg}",
            "is_active": "1",
            "is_unlimited_stock": "1",
        },
    )
    assert saved.status_code == 303, saved.text

    await session.refresh(other)
    assert other.name == "پلن دوم (ویرایش\u200cشده)"
    assert other.sort_order == 20
    assert (await session.get(type(plan), plan.id)).sort_order == 10
