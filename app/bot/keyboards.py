"""Style-aware keyboard building.

Why this module exists
----------------------
Telegram's *inline keyboard button* text is plain text — HTML is not allowed —
so a premium emoji on a button can only be delivered through the
``icon_custom_emoji_id`` field.  At the same time, the Persian copy deck writes
labels as ``"{e:cart} خرید سرویس"`` so translators never have to know emoji ids.

:class:`KeyboardBuilder` bridges the two:

* ``{e:key}`` present **and** a custom emoji id configured → drop the
  placeholder, set ``icon_custom_emoji_id``.
* ``{e:key}`` present **and** no custom id → replace it with the Unicode
  fallback so older clients still show something meaningful.
* ``{e:key}`` absent → the label is used verbatim.

Every keyboard also builds a "plain" twin (no colours, no custom emoji).  The
notifier falls back to it automatically if Telegram rejects the styled markup,
which is what happens against an older self-hosted Bot API server.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.services.appearance import appearance
from app.services.settings_store import button_styles_enabled, premium_emoji_enabled

_EMOJI_RE = re.compile(r"\{e:([a-z_]+)\}")

#: Telegram rejects an inline row with more than eight buttons (``aiogram`` raises
#: ``ValueError: Row size N is not allowed``), which would kill the whole screen.
#: Enforced for every keyboard, whatever ``columns`` the caller asked for.
MAX_ROW_BUTTONS = 8


@dataclass(slots=True)
class Button:
    """Declarative description of one button, resolved lazily."""

    visual_key: str | None = None
    text: str = ""
    callback_data: str | None = None
    url: str | None = None
    web_app: str | None = None
    switch_inline_query: str | None = None
    copy_text: str | None = None
    style: str | None = None
    icon_custom_emoji_id: str | None = None
    width: int = 1

    def merged_style(self) -> str | None:
        return self.style


@dataclass(slots=True)
class KB:
    """A markup plus the recipe to rebuild it without styling."""

    markup: InlineKeyboardMarkup
    _plain_factory: Callable[[], InlineKeyboardMarkup] | None = field(default=None, repr=False)

    def plain(self) -> InlineKeyboardMarkup | None:
        """Unstyled twin used when Telegram rejects colours/custom emoji."""
        if self._plain_factory is None:
            return None
        try:
            return self._plain_factory()
        except Exception:  # pragma: no cover - builder must never break sending
            return None

    def extend(self, other: KB) -> KB:
        """Append another keyboard's rows to this one.

        Screens that mix two layouts (a node's plans, then its sub-categories)
        build each part with the column count it deserves and join them here.
        Rows are copied verbatim, so the per-row limits were already enforced by
        the builders that produced them.
        """
        rows = list(self.markup.inline_keyboard) + list(other.markup.inline_keyboard)
        own_plain, other_plain = self.plain(), other.plain()
        if own_plain is None and other_plain is None:
            return KB(markup=InlineKeyboardMarkup(inline_keyboard=rows))
        plain_rows = list((own_plain or self.markup).inline_keyboard) + list(
            (other_plain or other.markup).inline_keyboard
        )
        return KB(
            markup=InlineKeyboardMarkup(inline_keyboard=rows),
            _plain_factory=lambda: InlineKeyboardMarkup(inline_keyboard=plain_rows),
        )


def _split_label(label: str, fallback: str) -> tuple[str, str | None, str]:
    """Return ``(clean_text, emoji_key, unicode_fallback)`` for a raw label."""
    match = _EMOJI_RE.search(label)
    if not match:
        return label.strip(), None, ""
    emoji_key = match.group(1)
    clean = _EMOJI_RE.sub("", label).strip()
    return clean, emoji_key, fallback


def _compose_label(clean: str, fallback: str, *, has_custom: bool) -> str:
    """Prefix the Unicode fallback only when no custom emoji will be attached."""
    if has_custom or not fallback:
        return clean
    # Avoid doubling when the translator already typed the same character.
    if clean.startswith(fallback):
        return clean
    return f"{fallback} {clean}".strip()


async def resolve_reply_label(visual_key: str, session=None) -> str:
    """The label a *reply* (physical) keyboard button shows.

    A reply keyboard carries no ``icon_custom_emoji_id``: Telegram would render
    the id as text.  The label is therefore the resolved text with its Unicode
    emoji, exactly as the inline builder would compose it with styling off — and
    it doubles as the route, because a press arrives as plain text.
    """
    resolved = await appearance.resolve(visual_key, session) if visual_key else None
    if resolved is None:
        return visual_key
    clean, inline_emoji, _fallback = _split_label(resolved.label or visual_key, resolved.emoji_fallback or "")
    fallback = resolved.emoji_fallback or ""
    if inline_emoji:
        emoji_visual = await appearance.resolve(f"emoji.{inline_emoji}", session)
        fallback = emoji_visual.emoji_fallback or fallback
    return _compose_label(clean, fallback, has_custom=False)


class KeyboardBuilder:
    """Async-friendly builder that resolves colours and premium emoji.

    ``columns`` is how many buttons share a row; rows break automatically, and
    never exceed :data:`MAX_ROW_BUTTONS`.  Call :meth:`row` for an explicit break.

    Usage::

        kb = KeyboardBuilder()
        await kb.add("menu.buy", callback=MenuCB(action="buy").pack())
        await kb.add("menu.my_services", callback=MenuCB(action="services").pack())
        kb.row()
        await kb.add("menu.support", callback=MenuCB(action="support").pack())
        markup = kb.build()
    """

    def __init__(
        self,
        *,
        session=None,
        styles: bool | None = None,
        emoji: bool | None = None,
        columns: int = 1,
    ) -> None:
        self.session = session
        self.styles = button_styles_enabled() if styles is None else styles
        self.emoji = premium_emoji_enabled() if emoji is None else emoji
        self.columns = min(MAX_ROW_BUTTONS, max(1, columns))
        self._rows: list[list[Button]] = []
        self._current: list[Button] = []

    # -- construction ------------------------------------------------------
    async def add(
        self,
        visual_key: str | None = None,
        *,
        text: str | None = None,
        callback: str | None = None,
        url: str | None = None,
        web_app: str | None = None,
        switch_inline_query: str | None = None,
        copy_text: str | None = None,
        style: str | None = None,
        icon_custom_emoji_id: str | None = None,
        emoji_key: str | None = None,
        width: int = 1,
        new_row: bool = False,
    ) -> KeyboardBuilder:
        """Append a button.

        ``text`` defaults to the label of ``visual_key``.  ``emoji_key``
        overrides which emoji from the catalog is used as the icon — the
        category tree, for instance, stores a per-category emoji key.
        """
        if new_row:
            self.row()
        if len(self._current) >= self.columns:
            self.row()

        resolved = await appearance.resolve(visual_key, self.session) if visual_key else None
        raw_text = text if text is not None else (resolved.label if resolved else "")
        resolved_style = style if style is not None else (resolved.style if resolved else None)
        resolved_icon = (
            icon_custom_emoji_id
            if icon_custom_emoji_id is not None
            else (resolved.icon_custom_emoji_id if resolved else None)
        )
        fallback = resolved.emoji_fallback if resolved else ""

        clean, inline_emoji, inline_fallback = _split_label(raw_text, fallback)
        chosen_emoji = emoji_key or inline_emoji
        if chosen_emoji:
            emoji_visual = await appearance.resolve(f"emoji.{chosen_emoji}", self.session)
            fallback = emoji_visual.emoji_fallback or inline_fallback
            if resolved_icon is None or emoji_key:
                resolved_icon = emoji_visual.icon_custom_emoji_id

        if not self.emoji:
            resolved_icon = None
        label = _compose_label(clean, fallback, has_custom=bool(resolved_icon))

        button = Button(
            visual_key=visual_key,
            text=label,
            callback_data=callback,
            url=url,
            web_app=web_app,
            switch_inline_query=switch_inline_query,
            copy_text=copy_text,
            style=resolved_style if self.styles else None,
            icon_custom_emoji_id=resolved_icon,
            width=width,
        )
        for _ in range(max(1, width)):
            self._current.append(button)
        if len(self._current) >= self.columns:
            self.row()
        return self

    async def add_many(self, specs: Iterable[dict]) -> KeyboardBuilder:
        for spec in specs:
            await self.add(**spec)
        return self

    def row(self) -> KeyboardBuilder:
        if self._current:
            self._rows.append(self._current)
            self._current = []
        return self

    # -- output ------------------------------------------------------------
    def build(self) -> KB:
        rows = self._snapshot_rows()

        def factory(*, styled: bool) -> InlineKeyboardMarkup:
            builder = InlineKeyboardBuilder()
            for row in rows:
                for button in row:
                    kwargs: dict[str, object] = {"text": button.text}
                    if button.callback_data:
                        kwargs["callback_data"] = button.callback_data
                    if button.url:
                        kwargs["url"] = button.url
                    if button.web_app:
                        from aiogram.types import WebAppInfo

                        kwargs["web_app"] = WebAppInfo(url=button.web_app)
                    if button.switch_inline_query is not None:
                        kwargs["switch_inline_query"] = button.switch_inline_query
                    if button.copy_text:
                        from aiogram.types import CopyTextButton

                        kwargs["copy_text"] = CopyTextButton(text=button.copy_text)
                    if styled:
                        if button.style:
                            kwargs["style"] = button.style
                        if button.icon_custom_emoji_id:
                            kwargs["icon_custom_emoji_id"] = button.icon_custom_emoji_id
                        text = button.text
                    else:
                        text = _fallback_text(button)
                        kwargs["text"] = text
                    builder.add(InlineKeyboardButton(**kwargs))  # type: ignore[arg-type]
            # One call for the whole keyboard: ``adjust`` rebuilds every row from all
            # buttons, so calling it per row would leave only the last width in force.
            if rows:
                builder.adjust(*[len(row) for row in rows])
            return builder.as_markup()

        markup = factory(styled=True)
        plain = factory(styled=False)
        same = markup.model_dump(exclude_none=True) == plain.model_dump(exclude_none=True)
        return KB(markup=markup, _plain_factory=None if same else lambda: factory(styled=False))

    def _snapshot_rows(self) -> list[list[Button]]:
        rows = [list(row) for row in self._rows]
        if self._current:
            rows.append(list(self._current))
        # Collapse the duplicated references created by ``width``.
        collapsed: list[list[Button]] = []
        for row in rows:
            seen: list[Button] = []
            for button in row:
                if not seen or seen[-1] is not button:
                    seen.append(button)
            collapsed.append(seen)
        # Last line of defence: a keyboard Telegram refuses is a screen that never
        # renders, so an over-wide row is split instead of raising.
        split: list[list[Button]] = []
        for row in collapsed:
            split.extend(row[i : i + MAX_ROW_BUTTONS] for i in range(0, len(row), MAX_ROW_BUTTONS))
        return split

    def __len__(self) -> int:
        return sum(len(row) for row in self._snapshot_rows())


def _fallback_text(button: Button) -> str:
    """Text used in the unstyled twin — keeps the Unicode emoji, drops ids."""
    return button.text


# ---------------------------------------------------------------------------
# Ready-made keyboards
# ---------------------------------------------------------------------------
async def back_row(session=None, *, visual_key: str = "menu.back", target: str = "main") -> KeyboardBuilder:
    from app.bot.callbacks import NavCB

    kb = KeyboardBuilder(session=session)
    await kb.add(visual_key, callback=NavCB(to=target).pack())
    return kb


async def pagination_row(
    session,
    *,
    section: str,
    page: int,
    has_prev: bool,
    has_next: bool,
) -> KeyboardBuilder:
    from app.bot.callbacks import PageCB

    kb = KeyboardBuilder(session=session)
    if has_prev:
        await kb.add("common.prev_page", callback=PageCB(section=section, page=page - 1).pack())
    if has_next:
        await kb.add("common.next_page", callback=PageCB(section=section, page=page + 1).pack())
    return kb


async def confirm_row(
    session,
    *,
    action: str,
    token: str = "",
    yes_key: str = "common.yes",
    no_key: str = "common.no",
) -> KeyboardBuilder:
    from app.bot.callbacks import ConfirmCB

    kb = KeyboardBuilder(session=session, columns=2)
    await kb.add(yes_key, callback=ConfirmCB(action=action, token=token).pack())
    await kb.add(no_key, callback="noop")
    return kb


def rows_to_specs(rows: Sequence[Sequence[dict]]) -> list[dict]:
    """Utility for building static keyboards declaratively."""
    specs: list[dict] = []
    for index, row in enumerate(rows):
        for button in row:
            spec = dict(button)
            if index > 0 and spec is row[0]:
                spec["new_row"] = True
            specs.append(spec)
    return specs


__all__ = [
    "KB",
    "Button",
    "KeyboardBuilder",
    "back_row",
    "confirm_row",
    "pagination_row",
    "resolve_reply_label",
    "rows_to_specs",
]
