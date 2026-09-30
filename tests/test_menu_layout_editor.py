"""The main-menu layout editor: the two endpoints, their two shapes, and the page.

The service rules already have their own module (``tests/test_menu_layout.py``);
what is worth pinning *here* is the seam the panel adds around them:

* the page renders the whole editor **server-side** — rows, the palette of
  buttons that are outside the menu, and the caps — so the editor is readable
  and testable before any script runs;
* a ``fetch`` save posts JSON and gets ``{"ok", "changed", "message"}``, while a
  plain form post (no JavaScript) posts repeated ``keys`` fields in row order
  and gets the usual 303 + flash;
* a refusal — an unknown key, a stale CSRF token, a body that is not the shape
  the editor sends — is always one calm Persian sentence: never a stack trace,
  never an HTTP status, and never a change to the stored menu.

Everything is driven through the real panel app, so the CSRF check, the auth
dependency and the redirect/flash plumbing are the ones an operator actually
hits.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlencode

import httpx
import pytest

from app.services import menu_layout
from app.services.appearance import CATALOG
from app.services.menu_layout import DEFAULT_ROWS

pytestmark = pytest.mark.db

BUTTONS_URL = "/panel/buttons"
LAYOUT_URL = "/panel/buttons/layout"
RESET_URL = "/panel/buttons/layout/reset"

#: The builder's own markup, as `app/web/templates/buttons.html` renders it.
BUILDER_MARKER = "data-menu-builder"
ROW_MARKER = '<div class="menu-row" data-menu-row>'
PALETTE_MARKER = '<div class="menu-palette" data-menu-palette>'

FETCH_HEADERS = {"X-Requested-With": "fetch", "Accept": "application/json"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _extract_csrf(html: str) -> str:
    """Scrape the hidden input the way a browser would submit it."""
    marker = 'name="csrf_token" value="'
    start = html.find(marker)
    assert start != -1, "the page carries no csrf_token input"
    start += len(marker)
    end = html.find('"', start)
    assert end > start
    return html[start:end]


def _palette_block(html: str) -> str:
    """The palette as rendered: everything outside the menu belongs there."""
    _board, separator, rest = html.partition(PALETTE_MARKER)
    assert separator, "the builder renders no [data-menu-palette]"
    # Chips are <span>s, so the first closing </div> ends the palette itself.
    return rest.split("</div>", 1)[0]


def _row_keys(html: str) -> list[list[str]]:
    """Every ``[data-menu-row]`` and the keys inside it, in document order."""
    board = html.partition(PALETTE_MARKER)[0]
    blocks = board.split(ROW_MARKER)[1:]
    assert blocks, "the builder renders no [data-menu-row]"
    return [re.findall(r'data-menu-key="([^"]+)"', block) for block in blocks]


def _palette_keys(html: str) -> list[str]:
    return re.findall(r'data-menu-key="([^"]+)"', _palette_block(html))


def _flat(rows) -> list[list[str]]:
    return [list(row.keys) for row in rows]


async def _layout(session) -> list[list[str]]:
    """The stored menu, read fresh so the route's own commit is visible."""
    session.expire_all()
    return _flat(await menu_layout.rows(session))


async def _hidden(session) -> set[str]:
    session.expire_all()
    return await menu_layout.hidden_keys(session)


async def _open_editor(client: httpx.AsyncClient) -> str:
    """Load the page and return its CSRF token, as a browser would."""
    page = await client.get(BUTTONS_URL)
    assert page.status_code == 200, page.text
    return _extract_csrf(page.text)


async def _fetch_save(client: httpx.AsyncClient, token: str, rows: list[list[str]]) -> httpx.Response:
    return await client.post(
        LAYOUT_URL,
        data={"csrf_token": token, "layout": json.dumps(rows)},
        headers=FETCH_HEADERS,
    )


async def _plain_save(client: httpx.AsyncClient, fields: list[tuple[str, str]]) -> httpx.Response:
    """A no-JavaScript post: repeated fields, in the order the browser sent them.

    The body is encoded here rather than passed as ``data=[…]``: httpx reads any
    non-mapping ``data`` as raw content, and the order *is* the contract — the
    route splits the rows on the ``row_break`` markers between the ``keys``.
    """
    return await client.post(
        LAYOUT_URL,
        content=urlencode(fields),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


async def _fetch_reset(client: httpx.AsyncClient, token: str) -> httpx.Response:
    return await client.post(RESET_URL, data={"csrf_token": token}, headers=FETCH_HEADERS)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------
async def test_the_page_renders_the_builder_with_every_current_key(signed_in_client: httpx.AsyncClient, owner) -> None:
    response = await signed_in_client.get(BUTTONS_URL)
    assert response.status_code == 200, response.text
    body = response.text

    assert BUILDER_MARKER in body
    assert f'data-menu-url="{LAYOUT_URL}"' in body
    assert f'action="{RESET_URL}"' in body
    # The caps come from the service, not from a number the template repeats.
    assert f'data-menu-max-rows="{menu_layout.MAX_ROWS}"' in body
    assert f'data-menu-max-buttons="{menu_layout.MAX_ROW_BUTTONS}"' in body
    assert 'data-csrf="' in body

    # Every button of the shipped menu is in its row, and nowhere else.
    assert _row_keys(body) == [list(row) for row in DEFAULT_ROWS]
    assert _palette_keys(body) == []

    # Each chip is draggable and reachable by keyboard, and carries its key.
    assert body.count("data-menu-handle") == sum(len(row) for row in DEFAULT_ROWS)
    assert 'class="drag-handle menu-chip__grip"' in body
    assert 'name="remove_key"' in body
    for key in (key for row in DEFAULT_ROWS for key in row):
        assert f'name="keys" value="{key}"' in body

    # The one-line promise the operator needs: removing hides, it does not delete.
    assert "پنهان" in body
    assert '<span class="code">Alt</span>' in body


async def test_the_page_renders_the_saved_arrangement(signed_in_client: httpx.AsyncClient, owner) -> None:
    """The editor is server-rendered: no script is needed to see the real menu."""
    token = await _open_editor(signed_in_client)
    target = [["menu.channels", "menu.rules"], ["menu.gift"]]

    assert (await _fetch_save(signed_in_client, token, target)).status_code == 200

    page = await signed_in_client.get(BUTTONS_URL)
    assert page.status_code == 200
    assert _row_keys(page.text) == target
    shipped = {key for row in DEFAULT_ROWS for key in row}
    assert sorted(_palette_keys(page.text)) == sorted(shipped - set(target[0]) - set(target[1]))


async def test_the_builder_needs_a_session(app_client: httpx.AsyncClient, owner) -> None:
    response = await app_client.post(LAYOUT_URL, data={"layout": '[["menu.buy"]]'})
    assert response.status_code == 303
    assert "/panel/login" in response.headers["location"]


# ---------------------------------------------------------------------------
# The fetch shape: JSON in, JSON out
# ---------------------------------------------------------------------------
async def test_a_fetch_save_reorders_the_menu(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    token = await _open_editor(signed_in_client)
    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]

    target = [["menu.my_services", "menu.buy"], ["menu.support"]]
    response = await _fetch_save(signed_in_client, token, target)

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["changed"] == 2
    assert "ذخیره" in payload["message"]
    assert await _layout(session) == target


async def test_a_fetch_save_of_an_unknown_key_is_refused(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    token = await _open_editor(signed_in_client)

    response = await _fetch_save(signed_in_client, token, [["menu.buy", "menu.definitely-not-real"]])

    assert response.status_code == 400
    payload = response.json()
    assert payload["ok"] is False
    assert payload["changed"] == 0
    # The operator's words: which button is wrong, and that it is not in the list.
    assert "menu.definitely-not-real" in payload["message"]
    assert "نیست" in payload["message"]
    assert "Traceback" not in payload["message"]
    assert "400" not in payload["message"]
    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]


async def test_a_fetch_save_of_a_body_that_is_not_the_editors_shape_is_refused(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    token = await _open_editor(signed_in_client)

    for broken in ('{"menu.buy": 1}', "not json at all", '["menu.buy"]'):
        response = await signed_in_client.post(
            LAYOUT_URL, data={"csrf_token": token, "layout": broken}, headers=FETCH_HEADERS
        )
        assert response.status_code == 400, broken
        payload = response.json()
        assert payload["ok"] is False
        assert payload["changed"] == 0
        assert "Traceback" not in payload["message"]

    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]


async def test_a_fetch_save_of_an_empty_menu_is_refused(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    token = await _open_editor(signed_in_client)

    response = await _fetch_save(signed_in_client, token, [])

    assert response.status_code == 400
    assert response.json()["ok"] is False
    assert "خالی" in response.json()["message"]
    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]


async def test_a_fetch_save_without_csrf_is_refused(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    response = await signed_in_client.post(
        LAYOUT_URL,
        data={"layout": json.dumps([["menu.buy"]])},
        headers=FETCH_HEADERS,
    )

    assert response.status_code == 403
    payload = response.json()
    assert payload["ok"] is False
    assert payload["changed"] == 0
    assert "منقضی" in payload["message"]
    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]


# ---------------------------------------------------------------------------
# The plain form shape: no JavaScript, still a working editor
# ---------------------------------------------------------------------------
async def test_a_plain_form_post_saves_and_redirects_with_a_flash(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    token = await _open_editor(signed_in_client)

    response = await _plain_save(
        signed_in_client,
        [
            ("csrf_token", token),
            ("keys", "menu.support"),
            ("row_break", "1"),
            ("keys", "menu.buy"),
            ("keys", "menu.my_services"),
        ],
    )

    assert response.status_code == 303
    assert response.headers["location"] == BUTTONS_URL
    assert "wggb_flash" in response.cookies
    assert await _layout(session) == [["menu.support"], ["menu.buy", "menu.my_services"]]

    # …and the flash reaches the operator as the usual toast.
    page = await signed_in_client.get(BUTTONS_URL)
    assert "چیدمان منوی اصلی ذخیره شد." in page.text


async def test_a_plain_form_post_takes_one_button_out_of_the_menu(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    """The ✕ is a real submit button, so removing a chip works without scripts."""
    token = await _open_editor(signed_in_client)
    label = CATALOG["menu.my_services"].label

    response = await _plain_save(
        signed_in_client,
        [
            ("csrf_token", token),
            ("keys", "menu.buy"),
            ("row_break", "1"),
            ("keys", "menu.my_services"),
            ("row_break", "1"),
            ("remove_key", "menu.my_services"),
        ],
    )

    assert response.status_code == 303
    assert response.headers["location"] == BUTTONS_URL
    assert await _layout(session) == [["menu.buy"]]
    assert "menu.my_services" in await _hidden(session)

    page = await signed_in_client.get(BUTTONS_URL)
    assert f"دکمه «{label}» از منوی اصلی برداشته شد" in page.text


async def test_a_plain_form_post_without_csrf_is_refused(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    response = await _plain_save(
        signed_in_client,
        [("keys", "menu.buy"), ("row_break", "1"), ("keys", "menu.support")],
    )

    assert response.status_code == 303
    assert response.headers["location"] == BUTTONS_URL
    assert "wggb_flash" in response.cookies
    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------
async def test_a_fetch_reset_restores_the_shipped_layout(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    token = await _open_editor(signed_in_client)
    assert (await _fetch_save(signed_in_client, token, [["menu.buy"]])).status_code == 200
    assert await _layout(session) == [["menu.buy"]]

    response = await _fetch_reset(signed_in_client, token)

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["changed"] == len(DEFAULT_ROWS)
    assert "پیش\u200cفرض" in payload["message"]
    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]
    assert await _hidden(session) == set()


async def test_a_plain_reset_redirects_with_a_flash(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    token = await _open_editor(signed_in_client)
    assert (await _fetch_save(signed_in_client, token, [["menu.support"]])).status_code == 200

    response = await signed_in_client.post(RESET_URL, data={"csrf_token": token})

    assert response.status_code == 303
    assert response.headers["location"] == BUTTONS_URL
    assert "wggb_flash" in response.cookies
    assert await _layout(session) == [list(row) for row in DEFAULT_ROWS]


async def test_a_reset_without_csrf_is_refused(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    token = await _open_editor(signed_in_client)
    assert (await _fetch_save(signed_in_client, token, [["menu.support"]])).status_code == 200

    response = await signed_in_client.post(RESET_URL, headers=FETCH_HEADERS)

    assert response.status_code == 403
    assert response.json()["ok"] is False
    assert await _layout(session) == [["menu.support"]]


# ---------------------------------------------------------------------------
# The palette
# ---------------------------------------------------------------------------
async def test_the_palette_offers_a_button_that_is_currently_hidden(
    signed_in_client: httpx.AsyncClient, owner, session
) -> None:
    """A removed button is only hidden: the editor has to offer it back."""
    token = await _open_editor(signed_in_client)
    kept = [["menu.buy"], ["menu.support"]]
    assert (await _fetch_save(signed_in_client, token, kept)).status_code == 200

    page = await signed_in_client.get(BUTTONS_URL)
    assert page.status_code == 200
    body = page.text

    assert _row_keys(body) == kept
    assert "menu.wallet" in _palette_keys(body)
    assert "menu.buy" not in _palette_keys(body)
    # It is marked as one the owner removed, and it is inert on the no-JS path.
    assert '<span class="badge badge--warning">حذف\u200cشده</span>' in _palette_block(body)
    assert 'name="keys" value="menu.wallet" disabled' in _palette_block(body)


async def test_a_palette_button_can_be_added_back(signed_in_client: httpx.AsyncClient, owner, session) -> None:
    """Dragging a chip out of the palette is just a row that lists it again."""
    token = await _open_editor(signed_in_client)
    assert (await _fetch_save(signed_in_client, token, [["menu.buy"]])).status_code == 200
    assert "menu.wallet" in await _hidden(session)

    response = await _fetch_save(signed_in_client, token, [["menu.buy", "menu.wallet"]])

    assert response.status_code == 200
    assert await _layout(session) == [["menu.buy", "menu.wallet"]]
    assert "menu.wallet" not in await _hidden(session)
