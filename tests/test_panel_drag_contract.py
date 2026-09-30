"""The drag-and-drop contract, checked against the HTML the panel really renders.

``panel.js`` reorders *the direct children of the element that carries*
``data-drag-list``.  Putting that attribute on a ``<table>`` instead of its
``<tbody>`` looks perfectly reasonable in the template and silently disables
dragging on every row — the first version of this feature shipped exactly that
way, and no attribute-presence assertion noticed.

So these tests parse the rendered page with :mod:`html.parser` and check the
structure the script depends on, not the existence of a string.
"""

from __future__ import annotations

from html.parser import HTMLParser

import httpx
import pytest

pytestmark = pytest.mark.db

#: Page path → the entity key its list must declare.
PAGES: dict[str, str] = {
    "/plans": "plans",
    "/categories": "categories",
    "/cards": "cards",
    "/channels": "channels",
    "/guides": "guides",
    "/panels": "panels",
}

VOID_TAGS = {"br", "img", "input", "meta", "link", "hr", "source", "col"}


class _Node:
    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: _Node | None) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node] = []
        self.parent = parent

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.tag} {self.attrs}>"


class _Dom(HTMLParser):
    """Just enough of a DOM to answer "what are this element's children?"."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document", {}, None)
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: (value or "") for key, value in attrs}, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {key: (value or "") for key, value in attrs}, self.current)
        self.current.children.append(node)

    def handle_endtag(self, tag: str) -> None:
        node = self.current
        while node is not self.root:
            if node.tag == tag:
                self.current = node.parent or self.root
                return
            node = node.parent or self.root


def _parse(html: str) -> _Dom:
    dom = _Dom()
    dom.feed(html)
    return dom


def _list_nodes(dom: _Dom, entity: str) -> list[_Node]:
    return [node for node in dom.root.walk() if node.attrs.get("data-drag-list") == entity]


# ---------------------------------------------------------------------------
# Seeding: a list only renders its draggable body when it has a row
# ---------------------------------------------------------------------------
async def _seed(session, entity: str) -> None:
    """One row of ``entity``, so the page renders its populated list."""
    from app.db.models import CardAccount, Channel, Guide, Panel, Plan, PlanCategory

    match entity:
        case "plans":
            session.add(Plan(name="پلن تست", price_rial=1_000_000, username_template="wg{tg}"))
        case "categories":
            session.add(PlanCategory(name="دسته تست", sort_order=10))
        case "cards":
            session.add(CardAccount(card_number="6037991111111111", holder_name="تست", sort_order=10))
        case "channels":
            session.add(Channel(chat_id="-1001234567890", title="کانال تست", sort_order=10))
        case "guides":
            session.add(Guide(title="آموزش تست", body="متن", sort_order=10))
        case "panels":
            session.add(Panel(name="نود تست", base_url="http://panel.test", api_token_encrypted="x", sort_order=10))
        case _:  # pragma: no cover - the parametrisation covers every case
            raise AssertionError(f"no seed for {entity!r}")
    await session.flush()
    await session.commit()


@pytest.mark.parametrize("path,entity", sorted(PAGES.items()))
async def test_the_draggable_container_owns_its_items(
    signed_in_client: httpx.AsyncClient, session, path: str, entity: str
) -> None:
    await _seed(session, entity)
    response = await signed_in_client.get(f"/panel{path}")
    assert response.status_code == 200
    dom = _parse(response.text)

    containers = _list_nodes(dom, entity)
    assert containers, f"{path} renders no [data-drag-list={entity!r}]"

    for container in containers:
        items = [child for child in container.children if child.attrs.get("data-drag-id")]
        # A table body whose rows live one level deeper than the container is the
        # bug this test exists for.
        nested = [node for node in container.walk() if node is not container and node.attrs.get("data-drag-id")]
        assert items or not nested, (
            f"{path}: [data-drag-list={entity!r}] is on <{container.tag}> but its items are nested "
            f"inside <{nested[0].parent.tag if nested and nested[0].parent else '?'}> — dragging would do nothing"
        )
        if container.tag == "table":
            raise AssertionError(f"{path}: the drag container must be the tbody, not the table")


@pytest.mark.parametrize("path,entity", sorted(PAGES.items()))
async def test_every_draggable_row_has_a_handle(
    signed_in_client: httpx.AsyncClient, session, path: str, entity: str
) -> None:
    """Without a handle the row is not draggable (and not keyboard-reachable)."""
    await _seed(session, entity)
    response = await signed_in_client.get(f"/panel{path}")
    dom = _parse(response.text)

    items = [node for node in dom.root.walk() if node.attrs.get("data-drag-id")]
    assert items, f"{path}: no draggable row rendered for {entity!r}"

    for item in items:
        handles = [node for node in item.walk() if "data-drag-handle" in node.attrs]
        assert handles, f"{path}: row {item.attrs['data-drag-id']} has no drag handle"
        assert handles[0].attrs.get("tabindex") is not None, (
            f"{path}: the handle must be focusable so Alt+Arrow can move the row"
        )


async def test_the_reorder_endpoint_is_reachable_from_the_panel(signed_in_client: httpx.AsyncClient) -> None:
    """The script posts to a fixed path; it must exist and refuse a bad request calmly."""
    response = await signed_in_client.post(
        "/panel/reorder",
        data={"entity": "plans", "ids": "1", "csrf_token": "wrong"},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 403
    assert response.json()["ok"] is False
