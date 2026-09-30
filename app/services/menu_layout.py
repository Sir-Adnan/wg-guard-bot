"""The main menu's layout: which buttons, in which row, in which order.

Two halves that used to be one hard-coded tuple:

* **the default** — :data:`DEFAULT_ROWS`, the layout a fresh installation ships
  with, in the same order the code has always drawn it;
* **the owner's version** — sparse :class:`~app.db.models.MenuLayoutEntry` rows
  that override, hide or add to the default.

Keeping the default in code is what makes an upgrade safe: a new release can add
a menu entry and every shop shows it, whether or not the owner ever opened the
editor.  Keeping the override sparse is what makes "reset" a delete.

The *meaning* of a key (which screen it opens) stays in the bot layer
(:data:`app.bot.menus.MENU_ACTIONS`) — this module is a service, and services may
not import the bot (``AGENTS.md`` §1.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationError
from app.core.logging import get_logger
from app.db.models import MenuLayoutEntry
from app.services.audit import audit

log = get_logger(__name__)

#: Rows of the shipped main menu, as appearance keys.
DEFAULT_ROWS: tuple[tuple[str, ...], ...] = (
    ("menu.buy", "menu.my_services"),
    ("menu.wallet", "menu.test"),
    ("menu.support", "menu.guides"),
    ("menu.profile", "menu.referral"),
    ("menu.gift", "menu.rules"),
    ("menu.channels",),
)

#: Buttons an owner may *add* to the main menu.  Everything here has a handler
#: behind it (``MenuCB`` action) and an appearance label, so the editor can offer
#: it and the bot can render it.
#:
#: The feature-gated ones (test, wallet, guides, gift) are also in
#: :data:`app.bot.menus.FEATURE_GATED`: hiding their feature in the panel removes
#: them from the menu even if the layout still lists them.
ADDABLE_KEYS: tuple[str, ...] = (
    "menu.buy",
    "menu.my_services",
    "menu.wallet",
    "menu.test",
    "menu.support",
    "menu.guides",
    "menu.profile",
    "menu.referral",
    "menu.gift",
    "menu.rules",
    "menu.channels",
)

#: How many buttons may share a row.  Telegram refuses a keyboard row of more
#: than eight, and two reads better on a phone — the editor is still allowed the
#: full range.
MAX_ROW_BUTTONS = 8

#: Hard cap on the number of rows, so a runaway payload cannot build a keyboard
#: taller than any screen.
MAX_ROWS = 12

_KNOWN_KEYS = frozenset(ADDABLE_KEYS) | {key for row in DEFAULT_ROWS for key in row}


@dataclass(frozen=True, slots=True)
class LayoutRow:
    """One row of the main menu after the owner's overrides were applied."""

    index: int
    keys: tuple[str, ...]

    @property
    def is_empty(self) -> bool:
        return not self.keys


def _normalise(rows: Sequence[Sequence[str]], *, strict: bool) -> list[list[str]]:
    """Trim, drop empties and duplicates, and refuse anything unknown.

    ``strict`` is the difference between "the operator sent this" (refuse an
    unknown key, so a typo cannot silently delete a button) and "read what is
    stored" (drop it, so an old row from a release that had an extra button does
    not break the menu).
    """
    out: list[list[str]] = []
    seen: set[str] = set()
    for row in rows:
        cleaned: list[str] = []
        for raw in row:
            key = str(raw or "").strip()
            if not key or key in seen:
                continue
            if key not in _KNOWN_KEYS:
                if strict:
                    raise ValidationError(f"دکمه «{key}» در فهرست دکمه‌های منو نیست.")
                log.warning("Ignoring unknown menu key %r from the stored layout", key)
                continue
            seen.add(key)
            cleaned.append(key)
        if cleaned:
            out.append(cleaned[:MAX_ROW_BUTTONS])
        if len(out) >= MAX_ROWS:
            break
    return out


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------
async def overrides(session: AsyncSession) -> dict[str, MenuLayoutEntry]:
    rows = (await session.execute(select(MenuLayoutEntry))).scalars()
    return {row.key: row for row in rows}


async def rows(session: AsyncSession | None) -> list[LayoutRow]:
    """The main menu as it should be drawn, defaults included.

    ``session=None`` returns the shipped layout: the notifier and the tests build
    keyboards without a database, and an empty menu would be worse than a
    default one.
    """
    if session is None:
        return [LayoutRow(index, tuple(row)) for index, row in enumerate(DEFAULT_ROWS)]

    stored = await overrides(session)
    if not stored:
        return [LayoutRow(index, tuple(row)) for index, row in enumerate(DEFAULT_ROWS)]

    # Start from the default, in place, and let the stored rows win.
    placed: dict[str, tuple[int, int]] = {}
    for row_index, row in enumerate(DEFAULT_ROWS):
        for position, key in enumerate(row):
            placed[key] = (row_index, position)

    hidden: set[str] = set()
    for key, entry in stored.items():
        if not entry.is_visible:
            hidden.add(key)
            placed.pop(key, None)
            continue
        placed[key] = (max(0, int(entry.row_index)), max(0, int(entry.position)))

    highest = 0
    buckets: dict[int, list[tuple[int, str]]] = {}
    for key, (row_index, position) in placed.items():
        highest = max(highest, row_index)
        buckets.setdefault(row_index, []).append((position, key))

    layout: list[list[str]] = []
    for row_index in range(highest + 1):
        entries = sorted(buckets.get(row_index, []), key=lambda item: (item[0], item[1]))
        layout.append([key for _position, key in entries])

    normalised = _normalise(layout, strict=False)
    return [LayoutRow(index, tuple(row)) for index, row in enumerate(normalised)]


async def hidden_keys(session: AsyncSession | None) -> set[str]:
    if session is None:
        return set()
    return {key for key, entry in (await overrides(session)).items() if not entry.is_visible}


async def editable_rows(session: AsyncSession | None = None) -> tuple[list[LayoutRow], set[str]]:
    """``(rows, hidden)`` for the editor.

    Hidden buttons are *not* part of the drawn menu, but the editor needs to show
    them somewhere the operator can drag them back.
    """
    return await rows(session), await hidden_keys(session)


async def available_keys(session: AsyncSession | None = None) -> list[str]:
    """Keys the editor may offer that are currently nowhere in the menu."""
    current = {key for row in await rows(session) for key in row.keys}
    return [key for key in ADDABLE_KEYS if key not in current]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------
async def save(session: AsyncSession, rows_payload: Sequence[Sequence[str]]) -> int:
    """Replace the stored layout with ``rows_payload``; returns rows written.

    The payload is the **whole** menu as the editor shows it.  Two consequences
    are easy to get wrong and are handled here:

    * a key that is in the shipped layout but *not* in the payload was removed by
      the operator, and needs an explicit hidden row — "absent" alone would fall
      back to the default and the button would reappear on the next save;
    * a key from :data:`ADDABLE_KEYS` that *is* in the payload is stored as a
      visible row, which is how a button gets added to the menu.
    """
    layout = _normalise(rows_payload, strict=True)
    if not layout:
        raise ValidationError("منوی اصلی نمی‌تواند خالی باشد.")

    placed = {key for row in layout for key in row}
    shipped = {key for row in DEFAULT_ROWS for key in row}

    await session.execute(delete(MenuLayoutEntry))
    for row_index, row in enumerate(layout):
        for position, key in enumerate(row):
            session.add(MenuLayoutEntry(key=key, row_index=row_index, position=position, is_visible=True))
    for key in sorted(shipped - placed):
        session.add(MenuLayoutEntry(key=key, row_index=MAX_ROWS - 1, position=0, is_visible=False))
    await session.flush()

    await audit.record(
        session,
        "menu.layout",
        entity="menu_layout",
        description=f"main menu layout saved ({len(layout)} rows)",
        meta={"rows": [list(row) for row in layout], "hidden": sorted(shipped - placed)},
    )
    log.info("Main menu layout saved: %s", layout)
    return len(layout)


async def hide(session: AsyncSession, key: str) -> None:
    """Keep ``key`` out of the menu without losing the feature behind it."""
    entry = await session.get(MenuLayoutEntry, key)
    if entry is None:
        entry = MenuLayoutEntry(key=key, row_index=MAX_ROWS - 1, position=0, is_visible=False)
        session.add(entry)
    else:
        entry.is_visible = False
    await session.flush()
    await audit.record(
        session, "menu.hide", entity="menu_layout", entity_id=key, description=f"menu button {key} hidden"
    )


async def reset(session: AsyncSession) -> None:
    """Back to the shipped layout."""
    await session.execute(delete(MenuLayoutEntry))
    await session.flush()
    await audit.record(session, "menu.reset", entity="menu_layout", description="main menu layout reset to default")


__all__ = [
    "ADDABLE_KEYS",
    "DEFAULT_ROWS",
    "MAX_ROWS",
    "MAX_ROW_BUTTONS",
    "LayoutRow",
    "available_keys",
    "editable_rows",
    "hidden_keys",
    "hide",
    "overrides",
    "reset",
    "rows",
    "save",
]
