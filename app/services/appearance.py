"""Visual catalog: button colours and premium (custom) emoji.

Two Bot API 9.4+ features are exposed here:

* ``InlineKeyboardButton.style`` — ``primary`` | ``success`` | ``danger`` | ``link``
* ``InlineKeyboardButton.icon_custom_emoji_id`` / ``<tg-emoji>`` entities

Everything is addressed by a stable string key (``btn.buy``, ``emoji.fire``) so
that handlers never hard-code colours or emoji ids.  Operators override the
defaults from the admin panel; the overrides live in the ``button_styles``
table and are cached in-process for speed.

Default styling degrades gracefully: if a key has no custom emoji configured,
the plain Unicode fallback is used instead, so the bot looks right out of the
box on every Telegram client.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache
from app.db.models import ButtonStyleConfig

VisualKind = Literal["button", "emoji"]
ButtonStyleValue = Literal["primary", "success", "danger", "link"]

STYLE_VALUES: tuple[str, ...] = ("primary", "success", "danger", "link")
GROUP_LABELS: dict[str, str] = {
    "menu": "منوی اصلی",
    "shop": "فروشگاه",
    "purchase": "فرآیند خرید",
    "service": "سرویس‌ها",
    "wallet": "کیف پول",
    "test": "سرویس تست",
    "support": "پشتیبانی",
    "admin": "مدیریت",
    "receipt": "رسیدها",
    "common": "عمومی",
    "emoji": "ایموجی‌ها",
}


@dataclass(frozen=True, slots=True)
class Visual:
    """A styleable element: either a keyboard button or an inline emoji."""

    key: str
    label: str
    kind: VisualKind
    group: str
    style: str | None = None
    emoji_fallback: str = ""
    emoji_key: str | None = None  # buttons borrow an emoji's custom id


@dataclass(slots=True)
class ResolvedVisual:
    key: str
    label: str = ""
    style: str | None = None
    icon_custom_emoji_id: str | None = None
    emoji_fallback: str = ""

    def button_kwargs(self, *, styles_enabled: bool = True, emoji_enabled: bool = True) -> dict[str, str]:
        """Extra kwargs for ``InlineKeyboardBuilder.button``."""
        out: dict[str, str] = {}
        if styles_enabled and self.style:
            out["style"] = self.style
        if emoji_enabled and self.icon_custom_emoji_id:
            out["icon_custom_emoji_id"] = self.icon_custom_emoji_id
        return out


# ---------------------------------------------------------------------------
# Emoji catalog (key -> Persian label + Unicode fallback)
# ---------------------------------------------------------------------------
_EMOJI_RAW: tuple[tuple[str, str, str], ...] = (
    ("back", "بازگشت", "🔙"),
    ("bell", "زنگ/اعلان", "🔔"),
    ("bulb", "ایده", "💡"),
    ("calendar", "تقویم", "📅"),
    ("cancel", "انصراف", "↩️"),
    ("card", "کارت بانکی", "💳"),
    ("cart", "سبد خرید", "🛒"),
    ("chart", "نمودار", "📊"),
    ("check", "تیک", "✅"),
    ("clock", "ساعت", "⏰"),
    ("close", "بستن", "✖️"),
    ("crown", "تاج/ویژه", "👑"),
    ("cross", "ضربدر", "❌"),
    ("device", "دستگاه", "📱"),
    ("download", "دانلود", "⬇️"),
    ("edit", "ویرایش", "✏️"),
    ("eye", "مشاهده", "👁"),
    ("fire", "داغ/محبوب", "🔥"),
    ("gift", "هدیه", "🎁"),
    ("heart", "قلب", "❤️"),
    ("home", "خانه", "🏠"),
    ("hourglass", "شنی/در انتظار", "⏳"),
    ("info", "اطلاعات", "ℹ️"),
    ("key", "کلید", "🔑"),
    ("link", "لینک", "🔗"),
    ("list", "فهرست", "📋"),
    ("lock", "قفل", "🔒"),
    ("megaphone", "بلندگو", "📣"),
    ("minus", "کاهش", "➖"),
    ("money", "پول", "💰"),
    ("panel", "پنل", "🖥"),
    ("party", "جشن", "🎉"),
    ("plus", "افزودن", "➕"),
    ("profile", "پروفایل", "👤"),
    ("qr", "کیوآر", "🔳"),
    ("question", "سؤال", "❓"),
    ("receipt", "رسید", "🧾"),
    ("refresh", "بروزرسانی", "🔄"),
    ("rocket", "موشک", "🚀"),
    ("search", "جست‌وجو", "🔍"),
    ("send", "ارسال", "📤"),
    ("server", "سرور", "🗄"),
    ("service", "سرویس", "📦"),
    ("settings", "تنظیمات", "⚙️"),
    ("shield", "سپر/امنیت", "🛡"),
    ("sparkle", "درخشش", "✨"),
    ("star", "ستاره", "⭐"),
    ("support", "پشتیبانی", "🎧"),
    ("telegram", "تلگرام", "✈️"),
    ("ticket", "تیکت", "🎫"),
    ("traffic", "ترافیک", "📶"),
    ("trash", "حذف", "🗑"),
    ("unlock", "بازکردن", "🔓"),
    ("users", "کاربران", "👥"),
    ("wallet", "کیف پول", "👛"),
    ("warn", "هشدار", "⚠️"),
)


# ---------------------------------------------------------------------------
# Button catalog: key -> (label, style, emoji key)
# ---------------------------------------------------------------------------
_BUTTON_RAW: tuple[tuple[str, str, str, str | None, str | None], ...] = (
    # main menu
    ("menu.buy", "خرید سرویس", "menu", "success", "cart"),
    ("menu.my_services", "سرویس‌های من", "menu", "primary", "service"),
    ("menu.wallet", "کیف پول", "menu", "primary", "wallet"),
    ("menu.test", "سرویس تست", "menu", "primary", "gift"),
    ("menu.support", "پشتیبانی", "menu", "primary", "support"),
    ("menu.profile", "حساب کاربری", "menu", "primary", "profile"),
    ("menu.guides", "آموزش اتصال", "menu", "primary", "bulb"),
    ("menu.gift", "کد هدیه", "menu", None, "gift"),
    ("menu.referral", "معرفی به دوستان", "menu", None, "users"),
    ("menu.rules", "قوانین و راهنما", "menu", None, "info"),
    ("menu.channels", "کانال‌های ما", "menu", None, "megaphone"),
    ("menu.back", "بازگشت", "menu", None, "back"),
    ("menu.main", "منوی اصلی", "menu", None, "home"),
    ("menu.refresh", "بروزرسانی", "menu", None, "refresh"),
    ("menu.cancel", "انصراف", "menu", "danger", "cancel"),
    ("menu.confirm", "تأیید", "menu", "success", "check"),
    ("menu.close", "بستن", "menu", None, "close"),
    ("menu.join", "عضویت در کانال", "menu", "primary", "megaphone"),
    ("menu.check_join", "عضو شدم", "menu", "success", "check"),
    # shop
    ("shop.plan", "پلن فروشگاه", "shop", "primary", "service"),
    ("shop.buy_now", "همین را می‌خواهم", "shop", "success", "cart"),
    ("shop.test_plan", "پلن تست", "shop", None, "gift"),
    ("shop.category", "دسته‌بندی", "shop", "primary", "list"),
    ("shop.featured", "پیشنهاد ویژه", "shop", "success", "fire"),
    ("shop.all_plans", "همه سرویس‌ها", "shop", None, "list"),
    # guides
    ("guide.section", "بخش آموزش", "shop", "primary", "bulb"),
    ("guide.read", "مطالعه", "shop", None, "eye"),
    # gift codes
    ("gift.redeem", "وارد کردن کد هدیه", "purchase", "success", "gift"),
    # purchase
    ("buy.method_card", "کارت به کارت", "purchase", "success", "card"),
    ("buy.method_wallet", "پرداخت از کیف پول", "purchase", "primary", "wallet"),
    ("buy.apply_discount", "کد تخفیف دارم", "purchase", "primary", "star"),
    ("buy.pay_now", "پرداخت", "purchase", "success", "money"),
    ("buy.send_receipt", "ارسال رسید", "purchase", "success", "receipt"),
    ("buy.retry", "تلاش دوباره", "purchase", "primary", "refresh"),
    ("buy.resume_order", "ادامه سفارش", "purchase", "primary", "hourglass"),
    ("buy.new_order", "سفارش تازه", "purchase", "success", "refresh"),
    # service
    ("service.renew", "تمدید سرویس", "service", "success", "refresh"),
    ("service.config", "دریافت کانفیگ", "service", "primary", "download"),
    ("service.qr", "نمایش QR", "service", "primary", "qr"),
    ("service.sub_link", "لینک اشتراک", "service", "primary", "link"),
    ("service.rotate", "تغییر کلیدها", "service", "danger", "lock"),
    ("service.add_device", "افزودن دستگاه", "service", "primary", "plus"),
    ("service.delete_device", "حذف دستگاه", "service", "danger", "trash"),
    ("service.extra_traffic", "خرید حجم اضافه", "service", "primary", "traffic"),
    ("service.manage", "مدیریت سرویس", "service", "primary", "settings"),
    ("service.autorenew", "بسته بعدی", "service", "primary", "refresh"),
    # wallet
    ("wallet.deposit", "افزایش موجودی", "wallet", "success", "plus"),
    ("wallet.custom_amount", "مبلغ دلخواه", "wallet", "primary", "edit"),
    ("wallet.history", "تاریخچه تراکنش‌ها", "wallet", None, "list"),
    # test service
    ("test.claim", "دریافت سرویس تست", "test", "success", "gift"),
    # support
    ("support.new_ticket", "ارسال پیام جدید", "support", "success", "send"),
    ("support.my_tickets", "تیکت‌های من", "support", "primary", "ticket"),
    ("support.reply", "پاسخ", "support", "primary", "send"),
    ("support.close_ticket", "بستن تیکت", "support", "danger", "close"),
    # receipts (staff)
    ("receipt.approve", "تأیید رسید", "receipt", "success", "check"),
    ("receipt.reject", "رد رسید", "receipt", "danger", "cross"),
    ("receipt.view_user", "اطلاعات کاربر", "receipt", "primary", "profile"),
    ("receipt.message_user", "پیام به کاربر", "receipt", "primary", "send"),
    # admin bot panel
    ("admin.stats", "آمار فروش", "admin", "primary", "chart"),
    ("admin.receipts", "رسید‌های در انتظار", "admin", "primary", "receipt"),
    ("admin.orders", "سفارش‌ها", "admin", "primary", "list"),
    ("admin.users", "کاربران", "admin", "primary", "users"),
    ("admin.panels", "پنل‌ها", "admin", "primary", "server"),
    ("admin.broadcast", "پیام همگانی", "admin", "primary", "megaphone"),
    ("admin.search", "جست‌وجوی کاربر", "admin", "primary", "search"),
    ("admin.web_panel", "پنل مدیریت وب", "admin", "link", "panel"),
    ("admin.export", "خروجی CSV", "admin", "primary", "download"),
    # common
    ("common.yes", "بله", "common", "success", "check"),
    ("common.no", "خیر", "common", "danger", "cross"),
    ("common.prev_page", "قبلی", "common", None, "back"),
    ("common.next_page", "بعدی", "common", None, "close"),
    ("common.skip", "رد کردن", "common", None, "cancel"),
    ("common.done", "تمام", "common", "success", "check"),
)


def _build_catalog() -> dict[str, Visual]:
    catalog: dict[str, Visual] = {}
    for key, label, fallback in _EMOJI_RAW:
        catalog[f"emoji.{key}"] = Visual(
            key=f"emoji.{key}", label=label, kind="emoji", group="emoji", emoji_fallback=fallback
        )
    for key, label, group, style, emoji_key in _BUTTON_RAW:
        catalog[key] = Visual(
            key=key,
            label=label,
            kind="button",
            group=group,
            style=style,
            emoji_key=f"emoji.{emoji_key}" if emoji_key else None,
        )
    return catalog


CATALOG: dict[str, Visual] = _build_catalog()

_EMOJI_PLACEHOLDER_RE = re.compile(r"\{e:([a-z_]+)\}")


def catalog_groups() -> dict[str, list[Visual]]:
    """Catalog grouped for the admin panel UI."""
    grouped: dict[str, list[Visual]] = {}
    for visual in CATALOG.values():
        grouped.setdefault(visual.group, []).append(visual)
    return grouped


# ---------------------------------------------------------------------------
# Runtime store (DB overrides + cache)
# ---------------------------------------------------------------------------
class AppearanceStore:
    """Resolves the effective style/emoji for every catalog key."""

    CACHE_KEY = "appearance:all"

    def __init__(self) -> None:
        self._overrides: dict[str, dict[str, str | None]] = {}

    async def load(self, session: AsyncSession, *, force: bool = False) -> dict[str, dict[str, str | None]]:
        if not force:
            cached = await cache.buttons_cache.get(self.CACHE_KEY)
            if cached is not None:
                self._overrides = cached
                return cached
        rows = list((await session.execute(select(ButtonStyleConfig))).scalars())
        data = {
            row.key: {
                "label": row.label,
                "style": row.style,
                "icon_custom_emoji_id": row.icon_custom_emoji_id,
                "emoji_fallback": row.emoji_fallback,
            }
            for row in rows
        }
        self._overrides = data
        await cache.buttons_cache.set(self.CACHE_KEY, data, ttl=120)
        return data

    def invalidate(self) -> None:
        cache.buttons_cache.invalidate(self.CACHE_KEY)

    async def resolve(self, key: str, session: AsyncSession | None = None) -> ResolvedVisual:
        """Effective visual for ``key`` (falls back to catalog defaults)."""
        if not self._overrides and session is not None:
            await self.load(session)

        spec = CATALOG.get(key)
        override = self._overrides.get(key, {})

        style = override.get("style") or (spec.style if spec else None)
        fallback = override.get("emoji_fallback") or (spec.emoji_fallback if spec else "")
        custom_id = override.get("icon_custom_emoji_id")
        label = override.get("label") or (spec.label if spec else key)

        # A button without its own custom emoji borrows the linked emoji's.
        if not custom_id and spec is not None and spec.emoji_key:
            linked = self._overrides.get(spec.emoji_key, {})
            custom_id = linked.get("icon_custom_emoji_id")
            fallback = fallback or (CATALOG[spec.emoji_key].emoji_fallback if spec.emoji_key in CATALOG else "")
            if not override.get("emoji_fallback"):
                fallback = linked.get("emoji_fallback") or fallback

        return ResolvedVisual(
            key=key,
            label=label,
            style=style if style in STYLE_VALUES else None,
            icon_custom_emoji_id=custom_id,
            emoji_fallback=fallback or "",
        )

    async def button_kwargs(
        self, key: str, session: AsyncSession | None = None, *, styles_enabled: bool = True, emoji_enabled: bool = True
    ) -> dict[str, str]:
        resolved = await self.resolve(key, session)
        return resolved.button_kwargs(styles_enabled=styles_enabled, emoji_enabled=emoji_enabled)

    async def render_emoji(self, key: str, session: AsyncSession | None = None) -> str:
        """``<tg-emoji>`` markup when a custom id is configured, else Unicode."""
        resolved = await self.resolve(f"emoji.{key}", session)
        if resolved.icon_custom_emoji_id:
            return f'<tg-emoji emoji-id="{resolved.icon_custom_emoji_id}">{resolved.emoji_fallback}</tg-emoji>'
        return resolved.emoji_fallback

    async def render_text(self, text: str, session: AsyncSession | None = None) -> str:
        """Replace every ``{e:key}`` placeholder in ``text``."""
        if "{e:" not in text:
            return text

        keys = set(_EMOJI_PLACEHOLDER_RE.findall(text))
        if not keys:
            return text
        if not self._overrides and session is not None:
            await self.load(session)

        replacements: dict[str, str] = {}
        for key in keys:
            spec = CATALOG.get(f"emoji.{key}")
            override = self._overrides.get(f"emoji.{key}", {})
            fallback = override.get("emoji_fallback") or (spec.emoji_fallback if spec else "")
            custom_id = override.get("icon_custom_emoji_id")
            if custom_id:
                replacements[f"{{e:{key}}}"] = f'<tg-emoji emoji-id="{custom_id}">{fallback}</tg-emoji>'
            else:
                replacements[f"{{e:{key}}}"] = fallback
        for placeholder, value in replacements.items():
            text = text.replace(placeholder, value)
        return text

    # -- mutations ---------------------------------------------------------
    async def set_visual(
        self,
        session: AsyncSession,
        key: str,
        *,
        label: str | None = None,
        style: str | None = None,
        icon_custom_emoji_id: str | None = None,
        emoji_fallback: str | None = None,
    ) -> ButtonStyleConfig:
        spec = CATALOG.get(key)
        row = await session.get(ButtonStyleConfig, key)
        if row is None:
            row = ButtonStyleConfig(
                key=key,
                label=label or (spec.label if spec else key),
                group=spec.group if spec else "common",
            )
            session.add(row)
        elif label is not None:
            row.label = label.strip() or (spec.label if spec else key)
        row.style = style if style in STYLE_VALUES else None
        row.icon_custom_emoji_id = (icon_custom_emoji_id or "").strip() or None
        if emoji_fallback is not None:
            row.emoji_fallback = emoji_fallback.strip() or None
        await session.flush()
        self.invalidate()
        await self.load(session, force=True)
        return row

    async def bulk_set(self, session: AsyncSession, entries: dict[str, dict[str, str | None]]) -> int:
        """Apply many overrides at once (used by the panel's save button)."""
        count = 0
        for key, values in entries.items():
            if key not in CATALOG:
                continue
            await self.set_visual(
                session,
                key,
                label=values.get("label"),
                style=values.get("style"),
                icon_custom_emoji_id=values.get("icon_custom_emoji_id"),
                emoji_fallback=values.get("emoji_fallback"),
            )
            count += 1
        self.invalidate()
        await self.load(session, force=True)
        return count

    async def reset(self, session: AsyncSession, keys: list[str] | None = None) -> int:
        stmt = delete(ButtonStyleConfig)
        if keys:
            stmt = stmt.where(ButtonStyleConfig.key.in_(keys))
        result = await session.execute(stmt)
        self.invalidate()
        self._overrides = {}
        return int(result.rowcount or 0)


#: Process-wide singleton.
appearance = AppearanceStore()


__all__ = [
    "CATALOG",
    "GROUP_LABELS",
    "STYLE_VALUES",
    "AppearanceStore",
    "ResolvedVisual",
    "Visual",
    "appearance",
    "catalog_groups",
]
