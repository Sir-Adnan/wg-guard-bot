"""The bot's plan and the node's plan: one product, two records.

The operator's question that produced these tests: "I created a plan in the bot
and a plan appeared in WG-Guard — and 50 GB became 53.7 GB there."  Both are
correct behaviour of different things:

* the **bot's plan** is the product (price, name, category, stock);
* the **node's plan** is the technical template a purchase commits against, so
  WG-Guard needs its own row.  The bot creates it on the first sale (or when the
  operator presses «به‌روزرسانی نود») and keeps the terms in step;
* the numbers differ because 50 × 1024³ bytes is printed as 53.7 GB by decimal
  display — hence the `shop.traffic_unit` switch these tests pin.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.money import GB_BINARY, GB_DECIMAL, set_gb_basis
from app.db.models import Plan
from app.panels.client import WGGuardClient
from app.services.settings_store import app_settings, apply_runtime_settings

pytestmark = pytest.mark.db


def _csrf(html: str) -> str:
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    assert start != -1, "the page carries no csrf_token input"
    start += len(marker)
    return html[start : html.find('"', start)]


async def _plan(session, *, traffic_gb: int = 50, name: str = "یک ماهه ۵۰ گیگ") -> Plan:
    plan = Plan(
        name=name,
        traffic_gb=traffic_gb,
        duration_days=30,
        price_rial=5_000_000,
        is_active=True,
        username_template="wg{tg}",
    )
    session.add(plan)
    await session.flush()
    return plan


async def _sync(client: httpx.AsyncClient, plan_id: int) -> httpx.Response:
    page = await client.get("/panel/plans")
    return await client.post(
        f"/panel/plans/{plan_id}/sync",
        data={"csrf_token": _csrf(page.text)},
    )


# ---------------------------------------------------------------------------
# The node plan exists because a purchase needs one
# ---------------------------------------------------------------------------
async def test_syncing_creates_the_node_plan_and_remembers_it(
    signed_in_client: httpx.AsyncClient, session, owner, panel_row
) -> None:
    plan = await _plan(session)
    await session.commit()
    assert plan.wg_plan_id is None

    response = await _sync(signed_in_client, plan.id)

    assert response.status_code == 303
    await session.refresh(plan)
    assert plan.wg_plan_id, "the node reference must be stored so it is reused, not duplicated"


async def test_the_node_stores_the_terms_the_operator_typed(
    signed_in_client: httpx.AsyncClient, session, owner, panel_row, wg_client: WGGuardClient
) -> None:
    """50 GB under the default decimal basis is exactly 50 GB on the node."""
    plan = await _plan(session)
    await session.commit()

    await _sync(signed_in_client, plan.id)
    await session.refresh(plan)

    remote = await wg_client.get_plan(plan.wg_plan_id or "")
    assert remote.traffic_limit_bytes == 50 * GB_DECIMAL
    assert remote.duration_seconds == 30 * 86400


async def test_switching_the_traffic_unit_changes_what_the_node_stores(
    signed_in_client: httpx.AsyncClient, session, owner, panel_row, wg_client: WGGuardClient
) -> None:
    """The setting is the whole answer to "why 53.7 instead of 50"."""
    plan = await _plan(session)
    await session.commit()
    await app_settings.load(session, force=True)
    await app_settings.set_many(session, {"shop.traffic_unit": "gb"})
    apply_runtime_settings()
    try:
        await _sync(signed_in_client, plan.id)
        await session.refresh(plan)
        same_ref = plan.wg_plan_id

        remote = await wg_client.get_plan(same_ref or "")
        assert remote.traffic_limit_bytes == 50 * GB_DECIMAL  # what "50 GB" means on the node panel

        # Switching back updates the *same* node plan rather than creating another.
        await app_settings.set_many(session, {"shop.traffic_unit": "gib"})
        apply_runtime_settings()
        await _sync(signed_in_client, plan.id)
        await session.refresh(plan)

        assert plan.wg_plan_id == same_ref
        assert (await wg_client.get_plan(same_ref or "")).traffic_limit_bytes == 50 * GB_BINARY
    finally:
        set_gb_basis(decimal=True)
        await app_settings.reset(session, "shop.traffic_unit")


async def test_syncing_twice_is_idempotent(
    signed_in_client: httpx.AsyncClient, session, owner, panel_row, wg_client: WGGuardClient
) -> None:
    """No duplicate plans on the node, however many times the button is pressed."""
    plan = await _plan(session)
    await session.commit()
    before = len(await wg_client.list_plans())

    await _sync(signed_in_client, plan.id)
    await session.refresh(plan)
    first = plan.wg_plan_id
    await _sync(signed_in_client, plan.id)
    await session.refresh(plan)

    assert plan.wg_plan_id == first
    # The mock node ships with seed plans of its own, so compare the count.
    assert len(await wg_client.list_plans()) == before + 1, "a second sync created a duplicate node plan"


async def test_a_plan_with_no_active_panel_explains_itself(
    signed_in_client: httpx.AsyncClient, session, owner, panel_row
) -> None:
    plan = await _plan(session)
    panel_row.is_active = False
    await session.commit()

    response = await _sync(signed_in_client, plan.id)

    assert response.status_code == 303, "a missing panel is a flash, never a 500"
    page = await signed_in_client.get(response.headers["location"])
    assert "پنل" in page.text


# ---------------------------------------------------------------------------
# The page says which node plan belongs to which product
# ---------------------------------------------------------------------------
async def test_the_list_labels_the_node_plan(signed_in_client: httpx.AsyncClient, session, owner, panel_row) -> None:
    plan = await _plan(session)
    other = await _plan(session, traffic_gb=7, name="۷ روزه ۷ گیگ")
    await session.commit()

    await _sync(signed_in_client, plan.id)
    page = await signed_in_client.get("/panel/plans")

    assert "پلن نود" in page.text
    assert "هنوز روی نود ساخته نشده" in page.text  # the plan that was never sold or synced
    await session.refresh(other)
    assert other.wg_plan_id is None


async def test_the_form_names_the_unit_in_force(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    page = await signed_in_client.get("/panel/plans")
    assert "۱۰۰۰" in page.text  # decimal GB matches the official API

    await app_settings.load(session, force=True)
    await app_settings.set_many(session, {"shop.traffic_unit": "gib"})
    apply_runtime_settings()
    try:
        page = await signed_in_client.get("/panel/plans")
        assert "۱۰۲۴" in page.text
    finally:
        set_gb_basis(decimal=True)
        await app_settings.reset(session, "shop.traffic_unit")


async def test_syncing_without_csrf_is_refused(signed_in_client: httpx.AsyncClient, session, owner, panel_row) -> None:
    plan = await _plan(session)
    await session.commit()

    response = await signed_in_client.post(f"/panel/plans/{plan.id}/sync", data={})

    assert response.status_code == 403
    await session.refresh(plan)
    assert plan.wg_plan_id is None, "a refused request must not touch the node"


async def test_an_anonymous_sync_is_refused(app_client: httpx.AsyncClient, session, panel_row) -> None:
    plan = await _plan(session)
    await session.commit()

    response = await app_client.post(f"/panel/plans/{plan.id}/sync", data={"csrf_token": "x"})

    assert response.status_code == 303
    assert "/panel/login" in response.headers["location"]
