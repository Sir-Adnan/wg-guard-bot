"""Every panel page, rendered end-to-end.

This is the integration net under the whole admin UI: it logs in through the
real cookie flow and asserts that each registered page answers 200 with a real
database behind it.  Anything the page modules import — services, models,
templates, filters — has to be correct for this file to pass.
"""

from __future__ import annotations

import httpx
import pytest

from app.core.security import hash_password
from app.db.models import Staff, StaffRole

pytestmark = pytest.mark.db

PAGES: tuple[str, ...] = (
    "/",
    "/plans",
    "/categories",
    "/cards",
    "/channels",
    "/discounts",
    "/gifts",
    "/guides",
    "/orders",
    "/receipts",
    "/services",
    "/users",
    "/tickets",
    "/buttons",
    "/panels",
    "/texts",
    "/settings",
    "/reports",
    "/broadcast",
    "/logs",
    "/staff",
    "/profile",
)

PASSWORD = "Owner-pass-123"


@pytest.fixture
async def owner(session):
    row = Staff(
        name="مالک تست",
        role=StaffRole.OWNER,
        login="owner",
        password_hash=hash_password(PASSWORD),
        receive_receipts=True,
        is_active=True,
    )
    session.add(row)
    await session.commit()
    return row


@pytest.fixture
async def app_client() -> httpx.AsyncClient:
    from app.web.app import create_app

    app = create_app(start_background=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://panel.test", follow_redirects=False) as client:
        yield client


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


async def test_login_page_is_public(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/panel/login")
    assert response.status_code == 200
    assert "ورود" in response.text


async def test_protected_page_redirects_to_login(app_client: httpx.AsyncClient) -> None:
    response = await app_client.get("/panel/plans")
    assert response.status_code == 303
    assert "/panel/login" in response.headers["location"]


async def test_wrong_password_is_rejected(app_client: httpx.AsyncClient, owner) -> None:
    response = await app_client.post("/panel/login", data={"login": "owner", "password": "nope"})
    assert response.status_code == 401
    assert "نادرست" in response.text


async def test_login_sets_a_session_cookie(app_client: httpx.AsyncClient, owner) -> None:
    response = await app_client.post("/panel/login", data={"login": "owner", "password": PASSWORD})
    assert response.status_code == 303
    assert "wggb_session" in response.cookies

    page = await app_client.get("/panel/")
    assert page.status_code == 200
    assert "داشبورد" in page.text


async def test_health_endpoints_are_public(app_client: httpx.AsyncClient) -> None:
    assert (await app_client.get("/healthz")).status_code == 200
    ready = await app_client.get("/readyz")
    assert ready.status_code in (200, 503)


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


async def _login(client: httpx.AsyncClient) -> None:
    response = await client.post("/panel/login", data={"login": "owner", "password": PASSWORD})
    assert response.status_code == 303, response.text


@pytest.mark.parametrize("path", PAGES)
async def test_every_page_renders(app_client: httpx.AsyncClient, owner, path: str) -> None:
    await _login(app_client)
    response = await app_client.get(f"/panel{path}")
    assert response.status_code == 200, f"{path} -> {response.status_code}: {response.text[:400]}"
    body = response.text
    assert "<html" in body
    # The layout must be applied, not a bare error page.
    assert "sidebar" in body
    assert "csrf" in body.lower() or "csrf_token" in body


async def test_pages_survive_query_parameters(app_client: httpx.AsyncClient, owner) -> None:
    await _login(app_client)
    cases = (
        "/panel/plans?page=1&size=10",
        "/panel/orders?status=completed&page=1",
        "/panel/receipts?status=pending",
        "/panel/services?status=active",
        "/panel/users?q=111&blocked=0",
        "/panel/logs?tab=audit",
        "/panel/guides?section=connect",
        "/panel/texts?group=menu",
        "/panel/reports?days=7",
    )
    for path in cases:
        response = await app_client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"


# ---------------------------------------------------------------------------
# CSRF
# ---------------------------------------------------------------------------


async def test_post_without_csrf_is_forbidden(app_client: httpx.AsyncClient, owner) -> None:
    await _login(app_client)
    response = await app_client.post("/panel/cards", data={"holder_name": "علی"})
    assert response.status_code == 403


async def test_csrf_token_from_the_page_is_accepted(app_client: httpx.AsyncClient, owner) -> None:
    await _login(app_client)
    page = await app_client.get("/panel/cards")
    token = _extract_csrf(page.text)
    assert token

    response = await app_client.post(
        "/panel/cards",
        data={
            "csrf_token": token,
            "card_number": "6037-9911-2233-4455",
            "holder_name": "علی رضایی",
            "bank_name": "بانک ملی",
            "sort_order": "0",
            "is_active": "1",
        },
    )
    assert response.status_code == 303

    listed = await app_client.get("/panel/cards")
    assert "علی رضایی" in listed.text
    assert "6037" in listed.text


def _extract_csrf(html: str) -> str | None:
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = html.find('"', start)
    return html[start:end] if end > start else None


async def test_flash_message_reaches_the_next_page(app_client: httpx.AsyncClient, owner) -> None:
    """Persian flash text must survive the cookie round trip (latin-1 safe)."""
    await _login(app_client)
    page = await app_client.get("/panel/cards")
    token = _extract_csrf(page.text)

    created = await app_client.post(
        "/panel/cards",
        data={
            "csrf_token": token,
            "card_number": "6104337712349876",
            "holder_name": "سارا محمدی",
            "sort_order": "1",
            "is_active": "1",
        },
    )
    assert created.status_code == 303
    assert "wggb_flash" in created.cookies

    follow_up = await app_client.get("/panel/cards")
    assert follow_up.status_code == 200
    assert "server-flash" in follow_up.text
    assert "data-message" in follow_up.text


# ---------------------------------------------------------------------------
# A full card-to-card round trip through the panel
# ---------------------------------------------------------------------------


async def test_receipt_approval_through_the_panel(
    app_client: httpx.AsyncClient, owner, customer, plan, panel_row, session
) -> None:
    """Customer pays by card → the owner approves → the order completes."""
    from app.db.models import ReceiptMedia, ReceiptStatus
    from app.services.orders import order_service
    from app.services.receipts import receipt_service

    order = await order_service.create(session, customer, plan)
    receipt = await receipt_service.create(
        session,
        customer,
        purpose="purchase",
        amount_rial=order.payable_rial,
        media=ReceiptMedia.PHOTO,
        file_id="panel-test-file",
        order=order,
    )
    await session.commit()

    await _login(app_client)
    queue = await app_client.get("/panel/receipts")
    assert queue.status_code == 200

    token = _extract_csrf(queue.text)
    approved = await app_client.post(f"/panel/receipts/{receipt.id}/approve", data={"csrf_token": token})
    assert approved.status_code == 303

    await session.refresh(receipt)
    await session.refresh(order)
    assert receipt.status is ReceiptStatus.APPROVED
    assert order.status.value in ("paid", "provisioning", "completed")
