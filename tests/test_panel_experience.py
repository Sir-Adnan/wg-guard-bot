"""Shared shell, permissions and secure forms survive the visual redesign."""

import re
from html.parser import HTMLParser

import pytest

pytestmark = pytest.mark.db


class _Navigation(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and "data-nav-link" in attrs:
            self.links.append(attrs.get("href"))


def _csrf(response):
    match = re.search(r'data-csrf="([^"]+)"', response.text)
    assert match
    return match.group(1)


async def test_shared_shell_has_accessible_navigation_and_local_assets(signed_in_client, owner):
    response = await signed_in_client.get("/panel/plans")
    assert response.status_code == 200
    assert 'data-theme="light" data-style="atelier"' in response.text
    assert 'href="#main-content"' in response.text
    assert 'id="command-dialog"' in response.text and 'id="confirm-dialog"' in response.text
    assert 'aria-controls="sidebar"' in response.text
    assert response.text.index("theme.js") < response.text.index("panel.css")
    font = await signed_in_client.get("/panel/static/fonts/Vazirmatn-Variable.woff2")
    assert font.status_code == 200 and font.content[:4] == b"wOF2"


async def test_profile_keeps_authenticated_sidebar_and_owner_navigation(signed_in_client, owner):
    response = await signed_in_client.get("/panel/profile")
    nav = _Navigation()
    nav.feed(response.text)
    assert "/panel/staff" in nav.links
    assert owner.name in response.text
    assert 'action="/panel/logout"' in response.text


async def test_logout_requires_csrf_and_valid_logout_clears_session(signed_in_client, owner):
    page = await signed_in_client.get("/panel/")
    response = await signed_in_client.post("/panel/logout", data={})
    assert response.status_code == 403
    assert (await signed_in_client.get("/panel/profile")).status_code == 200
    response = await signed_in_client.post("/panel/logout", data={"csrf_token": _csrf(page)})
    assert response.status_code == 303
    assert (await signed_in_client.get("/panel/profile")).status_code == 303


async def test_support_navigation_lists_only_readable_routes(app_client, session):
    from app.core.security import hash_password
    from app.db.models import Staff, StaffRole

    staff = Staff(
        login="ui-support", name="پشتیبان", role=StaffRole.SUPPORT, password_hash=hash_password("Support-only-2026")
    )
    session.add(staff)
    await session.commit()
    response = await app_client.post("/panel/login", data={"login": staff.login, "password": "Support-only-2026"})
    assert response.status_code == 303
    response = await app_client.get("/panel/")
    nav = _Navigation()
    nav.feed(response.text)
    assert {"/panel/orders", "/panel/receipts", "/panel/tickets"} <= set(nav.links)
    assert "/panel/staff" not in nav.links and "/panel/settings" not in nav.links and "/panel/plans" not in nav.links
    for path in nav.links:
        assert (await app_client.get(path)).status_code == 200
