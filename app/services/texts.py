"""Bot copy: shipped catalog + operator overrides.

Texts live in ``app/locales/<locale>.json`` so the default Persian wording is
reviewable, diffable and translatable.  Operators can override any single key
from the admin panel; overrides are stored in ``bot_texts`` and marked
``is_custom`` so a "reset to default" is always possible.

Rendering pipeline for every outgoing message::

    catalog/override -> {e:fire} premium emoji substitution -> str.format(**ctx)
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache
from app.core.logging import get_logger
from app.db.models import BotText
from app.services.appearance import appearance

log = get_logger(__name__)

LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"
DEFAULT_LOCALE = "fa"
_CACHE_KEY = "texts:overrides"

GROUP_LABELS: dict[str, str] = {
    "menu": "منو و دکمه‌ها",
    "start": "شروع و خوش‌آمدگویی",
    "shop": "فروشگاه",
    "buy": "خرید",
    "receipt": "رسید پرداخت",
    "service": "سرویس‌ها",
    "wallet": "کیف پول",
    "test": "سرویس تست",
    "support": "پشتیبانی",
    "profile": "حساب کاربری",
    "error": "خطاها",
    "common": "عمومی",
    "admin": "مدیریت",
    "other": "سایر",
}

_RTL_MARK = "\u200f"


@lru_cache(maxsize=4)
def load_catalog(locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    """Read the shipped catalog for ``locale`` (cached, never raises)."""
    path = LOCALES_DIR / f"{locale}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.error("Locale file missing: %s", path)
        return {}
    except json.JSONDecodeError as exc:
        log.error("Locale file %s is not valid JSON: %s", path, exc)
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def group_of(key: str) -> str:
    prefix = key.split(".", 1)[0]
    return prefix if prefix in GROUP_LABELS else "other"


class _SafeDict(dict):
    """``str.format_map`` helper that never raises on a missing placeholder."""

    def __missing__(self, key: str) -> str:
        log.warning("Missing text placeholder %r", key)
        return "—"


class TextStore:
    """Resolves text keys, applying database overrides on top of the catalog."""

    def __init__(self, locale: str = DEFAULT_LOCALE) -> None:
        self.locale = locale
        self._overrides: dict[str, str] = {}
        self._loaded = False

    # -- lifecycle ---------------------------------------------------------
    @property
    def catalog(self) -> dict[str, str]:
        return load_catalog(self.locale)

    def invalidate(self) -> None:
        self._loaded = False
        self._overrides = {}
        cache.texts_cache.invalidate(_CACHE_KEY)

    async def load(self, session: AsyncSession, *, force: bool = False) -> dict[str, str]:
        if self._loaded and not force:
            return self._overrides
        if not force:
            cached = await cache.texts_cache.get(_CACHE_KEY)
            if cached is not None:
                self._overrides = cached
                self._loaded = True
                return cached
        rows = list((await session.execute(select(BotText).where(BotText.is_custom.is_(True)))).scalars())
        self._overrides = {row.key: row.value for row in rows}
        self._loaded = True
        await cache.texts_cache.set(_CACHE_KEY, self._overrides, ttl=120)
        return self._overrides

    # -- readers -----------------------------------------------------------
    def raw(self, key: str, default: str | None = None) -> str:
        """Unrendered text (still contains ``{placeholders}`` and ``{e:...}``)."""
        if key in self._overrides:
            return self._overrides[key]
        catalog = self.catalog
        if key in catalog:
            return catalog[key]
        if default is not None:
            return default
        log.warning("Unknown text key: %s", key)
        return key

    async def get(self, key: str, session: AsyncSession | None = None, **context: Any) -> str:
        """Fully rendered text: premium emoji + placeholders."""
        text = self.raw(key)
        if session is not None and not self._loaded:
            await self.load(session)
        text = await appearance.render_text(text, session)
        return self.format(text, **context)

    @staticmethod
    def format(text: str, **context: Any) -> str:
        if "{" not in text:
            return text
        try:
            return text.format_map(_SafeDict(context))
        except (IndexError, ValueError) as exc:
            log.warning("Text formatting failed (%s) — sending unformatted copy", exc)
            return text

    def all_defaults(self) -> dict[str, str]:
        return dict(self.catalog)

    async def effective(self, session: AsyncSession) -> dict[str, str]:
        """Catalog merged with overrides — what the bot actually sends."""
        await self.load(session)
        merged = self.all_defaults()
        merged.update(self._overrides)
        return merged

    async def grouped(self, session: AsyncSession) -> dict[str, list[tuple[str, str, bool]]]:
        """``{group: [(key, value, is_custom), ...]}`` for the admin editor."""
        await self.load(session)
        out: dict[str, list[tuple[str, str, bool]]] = {}
        for key, value in sorted((await self.effective(session)).items()):
            group = group_of(key)
            out.setdefault(group, []).append((key, value, key in self._overrides))
        return out

    # -- writers -----------------------------------------------------------
    async def set_text(self, session: AsyncSession, key: str, value: str) -> None:
        default = self.catalog.get(key, "")
        row = await session.get(BotText, key)
        is_custom = value != default
        if row is None:
            row = BotText(key=key, value=value, is_custom=is_custom)
            session.add(row)
        else:
            row.value = value
            row.is_custom = is_custom
        await session.flush()
        if is_custom:
            self._overrides[key] = value
        else:
            self._overrides.pop(key, None)
        cache.texts_cache.invalidate(_CACHE_KEY)

    async def set_many(self, session: AsyncSession, values: dict[str, str]) -> int:
        count = 0
        for key, value in values.items():
            await self.set_text(session, key, value)
            count += 1
        return count

    async def reset_text(self, session: AsyncSession, key: str) -> str:
        row = await session.get(BotText, key)
        if row is not None:
            await session.delete(row)
            await session.flush()
        self._overrides.pop(key, None)
        cache.texts_cache.invalidate(_CACHE_KEY)
        return self.catalog.get(key, "")


#: Process-wide singleton used by handlers and the panel.
texts = TextStore()


async def t(key: str, session: AsyncSession | None = None, **context: Any) -> str:
    """Short alias used inside handlers: ``await t("buy.success", order=...)``."""
    return await texts.get(key, session, **context)


def html_escape(value: Any) -> str:
    """Escape user-controlled text before embedding it in an HTML message."""
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_tags(text: str) -> str:
    """Remove the HTML subset used by the bot (for logs and CSV exports)."""
    return re.sub(r"<[^>]+>", "", text)


ALLOWED_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "strike",
    "del",
    "code",
    "pre",
    "a",
    "tg-emoji",
    "blockquote",
    "span",
}


def looks_like_html(text: str) -> bool:
    return bool(re.search(r"</?(?:b|i|u|s|code|pre|a|tg-emoji|blockquote)\b", text))


__all__ = [
    "ALLOWED_TAGS",
    "DEFAULT_LOCALE",
    "GROUP_LABELS",
    "LOCALES_DIR",
    "TextStore",
    "group_of",
    "html_escape",
    "load_catalog",
    "looks_like_html",
    "strip_tags",
    "t",
    "texts",
]
