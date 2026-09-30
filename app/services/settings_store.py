"""Runtime settings kept in the database and edited from the admin panel.

Environment variables describe *deployment* concerns (secrets, hostnames,
database); the settings in this module describe *shop behaviour* that an
operator changes without redeploying — and that is why they live in Postgres
and not in ``.env``.

Every key is declared once in :data:`SPECS`; the admin panel renders itself from
that declaration, so adding a new option is a one-line change here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache
from app.core.config import settings as env
from app.core.logging import get_logger
from app.db.models import Setting

log = get_logger(__name__)

ValueType = Literal["str", "text", "int", "float", "bool", "money", "choice", "channel_list"]

GROUP_LABELS: dict[str, str] = {
    "shop": "فروشگاه",
    "payment": "پرداخت",
    "test": "سرویس تست",
    "membership": "عضویت اجباری",
    "notify": "اعلان‌ها",
    "appearance": "ظاهر و دکمه‌ها",
    "advanced": "پیشرفته",
}


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    label: str
    group: str
    type: ValueType = "str"
    default: Any = ""
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    minimum: float | None = None
    maximum: float | None = None


def _specs() -> dict[str, SettingSpec]:
    items = [
        # -- shop ----------------------------------------------------------
        SettingSpec(
            "shop.name", "نام فروشگاه", "shop", "str", env.app_name, "در متن‌های ربات به‌جای {shop} نمایش داده می‌شود."
        ),
        SettingSpec(
            "shop.support_username",
            "آی‌دی پشتیبانی",
            "shop",
            "str",
            "",
            "بدون @ وارد کنید؛ در دکمه پشتیبانی استفاده می‌شود.",
        ),
        SettingSpec("shop.support_hours", "ساعات پاسخ‌گویی", "shop", "str", "هر روز ۹ تا ۲۳"),
        SettingSpec(
            "shop.traffic_unit",
            "واحد حجم",
            "shop",
            "choice",
            "gib",
            "یک گیگابایت چند بایت باشد؟ پنل خود WG-Guard حجم را بر پایه ۱۰۰۰ نشان می‌دهد، "
            "پس اگر پلن ۵۰ گیگی در ربات آن‌جا ۵۳.۷ گیگابایت دیده می‌شود، گزینه «۱۰۰۰» را انتخاب کنید. "
            "تغییر این گزینه فقط روی پلن‌هایی اثر دارد که بعد از آن به نود فرستاده شوند.",
            choices=(("gib", "۱۰۲۴ (گیبی‌بایت — پیش‌فرض)"), ("gb", "۱۰۰۰ (گیگابایت اعشاری، مثل پنل)")),
        ),
        SettingSpec(
            "shop.maintenance",
            "حالت تعمیر",
            "shop",
            "bool",
            False,
            "با فعال شدن، فقط مدیران می‌توانند از ربات استفاده کنند.",
        ),
        SettingSpec(
            "shop.maintenance_message", "پیام حالت تعمیر", "shop", "text", "", "خالی بگذارید تا متن پیش‌فرض استفاده شود."
        ),
        SettingSpec(
            "shop.receipt_expire_minutes",
            "مهلت ارسال رسید (دقیقه)",
            "shop",
            "int",
            env.receipt_expire_minutes,
            minimum=5,
            maximum=1440,
        ),
        SettingSpec(
            "shop.min_deposit_rial", "حداقل شارژ کیف پول", "shop", "money", env.min_deposit_rial, "به تومان وارد کنید."
        ),
        SettingSpec(
            "shop.referral_percent", "پاداش معرفی (درصد)", "shop", "float", env.referral_percent, minimum=0, maximum=50
        ),
        SettingSpec("shop.referral_enabled", "فعال بودن معرفی دوستان", "shop", "bool", True),
        SettingSpec(
            "shop.cashback_percent",
            "کش‌بک خرید (درصد)",
            "shop",
            "float",
            0.0,
            minimum=0,
            maximum=30,
            help="درصدی از هر خرید موفق که خودکار به کیف پول کاربر برمی‌گردد.",
        ),
        SettingSpec("shop.gift_enabled", "کد هدیه", "shop", "bool", True),
        SettingSpec("shop.guides_enabled", "بخش آموزش اتصال", "shop", "bool", True),
        SettingSpec("shop.show_featured", "نمایش «پیشنهاد ویژه»", "shop", "bool", True),
        SettingSpec(
            "shop.show_categories",
            "نمایش دسته‌بندی‌ها",
            "shop",
            "bool",
            True,
            help="اگر دسته‌بندی نساخته باشید، فهرست ساده پلن‌ها نمایش داده می‌شود.",
        ),
        # -- payment -------------------------------------------------------
        SettingSpec("payment.card_enabled", "پرداخت کارت به کارت", "payment", "bool", env.card_to_card_enabled),
        SettingSpec("payment.wallet_enabled", "پرداخت از کیف پول", "payment", "bool", env.wallet_enabled),
        SettingSpec("payment.discount_enabled", "کد تخفیف", "payment", "bool", True),
        SettingSpec(
            "payment.receipt_requires_tracking",
            "الزام شماره پیگیری",
            "payment",
            "bool",
            False,
            "اگر فعال باشد، رسید بدون شماره پیگیری ثبت نمی‌شود.",
        ),
        # -- test service --------------------------------------------------
        SettingSpec("test.enabled", "فعال بودن سرویس تست", "test", "bool", env.test_service_enabled),
        SettingSpec("test.traffic_gb", "حجم سرویس تست (گیگ)", "test", "int", 1, minimum=1, maximum=100),
        SettingSpec("test.duration_days", "مدت سرویس تست (روز)", "test", "int", 1, minimum=1, maximum=30),
        SettingSpec(
            "test.cooldown_days",
            "فاصله مجاز (روز)",
            "test",
            "int",
            env.test_service_cooldown_days,
            minimum=0,
            maximum=3650,
            help="صفر یعنی محدودیت زمانی ندارد.",
        ),
        SettingSpec("test.device_limit", "تعداد دستگاه", "test", "int", 1, minimum=1, maximum=10),
        SettingSpec("test.panel_id", "پنل سرویس تست", "test", "int", 0, "۰ یعنی انتخاب خودکار."),
        # -- membership ----------------------------------------------------
        SettingSpec("membership.enabled", "عضویت اجباری", "membership", "bool", False),
        SettingSpec(
            "membership.recheck_minutes", "فاصله بررسی مجدد (دقیقه)", "membership", "int", 30, minimum=1, maximum=1440
        ),
        # -- notifications -------------------------------------------------
        SettingSpec("notify.admin_new_order", "اعلان سفارش جدید", "notify", "bool", True),
        SettingSpec("notify.admin_new_receipt", "اعلان رسید جدید", "notify", "bool", True),
        SettingSpec("notify.admin_new_ticket", "اعلان تیکت جدید", "notify", "bool", True),
        SettingSpec("notify.admin_panel_errors", "اعلان خطای پنل", "notify", "bool", True),
        SettingSpec("notify.admin_new_user", "اعلان کاربر جدید", "notify", "bool", False),
        SettingSpec("notify.user_expiry", "یادآوری انقضا به کاربر", "notify", "bool", True),
        SettingSpec("notify.user_traffic", "هشدار مصرف حجم", "notify", "bool", True),
        SettingSpec("notify.expiry_days", "یادآوری چند روز قبل", "notify", "int", 3, minimum=1, maximum=30),
        # -- appearance ----------------------------------------------------
        SettingSpec(
            "appearance.button_styles",
            "رنگ‌بندی دکمه‌ها",
            "appearance",
            "bool",
            True,
            "نیازمند Bot API 9.4+ و کلاینت‌های بروزرسانی‌شده.",
        ),
        SettingSpec(
            "appearance.premium_emoji",
            "ایموجی پرمیوم",
            "appearance",
            "bool",
            True,
            "برای دکمه‌ها نیاز به خرید یوزرنیم از Fragment دارد.",
        ),
        SettingSpec("appearance.plan_columns", "تعداد پلن در هر ردیف", "appearance", "int", 1, minimum=1, maximum=2),
        SettingSpec("appearance.show_plan_price_in_list", "نمایش قیمت در فهرست پلن‌ها", "appearance", "bool", True),
        SettingSpec(
            "appearance.reply_keyboard",
            "کیبورد فیزیکی",
            "appearance",
            "bool",
            False,
            "یک منوی متنی زیر کادر پیام نشان می‌دهد که بعد از بستن ربات هم می‌ماند. "
            "منوی شیشه‌ای سر جای خودش است و هر دو با هم کار می‌کنند.",
        ),
        # -- advanced ------------------------------------------------------
        SettingSpec(
            "advanced.panel_selection",
            "روش انتخاب پنل",
            "advanced",
            "choice",
            "auto",
            choices=(("auto", "خودکار (کم‌بارترین)"), ("priority", "بر اساس اولویت"), ("round_robin", "چرخشی")),
            help="در حالت خودکار، پنل با کمترین تعداد سرویس انتخاب می‌شود.",
        ),
        SettingSpec(
            "advanced.subscription_base_url",
            "آدرس عمومی لینک اشتراک",
            "advanced",
            "str",
            env.subscription_base_url,
            "خالی بگذارید تا از آدرس خود پنل استفاده شود.",
        ),
        SettingSpec(
            "advanced.max_open_orders", "حداکثر سفارش باز هر کاربر", "advanced", "int", 3, minimum=1, maximum=20
        ),
        SettingSpec("advanced.welcome_media", "عکس خوش‌آمدگویی (file_id)", "advanced", "str", ""),
        SettingSpec(
            "advanced.log_channel_id", "کانال لاگ", "advanced", "str", "", "شناسه عددی کانال برای ثبت رویداد‌های مهم."
        ),
    ]
    return {spec.key: spec for spec in items}


SPECS: dict[str, SettingSpec] = _specs()

SETTING_KEYS = tuple(SPECS.keys())
_CACHE_KEY = "settings:all"


def spec_groups() -> dict[str, list[SettingSpec]]:
    grouped: dict[str, list[SettingSpec]] = {}
    for spec in SPECS.values():
        grouped.setdefault(spec.group, []).append(spec)
    return grouped


class SettingsStore:
    """Cached accessor for the ``settings`` table."""

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._loaded = False

    # -- lifecycle ---------------------------------------------------------
    async def load(self, session: AsyncSession, *, force: bool = False) -> dict[str, Any]:
        if not force and self._loaded:
            return self._values
        if not force:
            cached = await cache.settings_cache.get(_CACHE_KEY)
            if cached is not None:
                self._values = cached
                self._loaded = True
                return cached

        rows = list((await session.execute(select(Setting))).scalars())
        values = {spec.key: spec.default for spec in SPECS.values()}
        for row in rows:
            values[row.key] = row.value
        self._values = values
        self._loaded = True
        await cache.settings_cache.set(_CACHE_KEY, values, ttl=120)
        return values

    def invalidate(self) -> None:
        self._loaded = False
        self._values = {}
        cache.settings_cache.invalidate(_CACHE_KEY)

    # -- readers -----------------------------------------------------------
    def _raw(self, key: str) -> Any:
        if key in self._values:
            return self._values[key]
        spec = SPECS.get(key)
        return spec.default if spec else None

    def get(self, key: str, default: Any = None) -> Any:
        value = self._raw(key)
        if value is None and default is not None:
            return default
        return value

    def get_str(self, key: str, default: str = "") -> str:
        value = self._raw(key)
        return str(value) if value is not None else default

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self._raw(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "بله")

    def get_int(self, key: str, default: int = 0) -> int:
        value = self._raw(key)
        try:
            return int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def get_float(self, key: str, default: float = 0.0) -> float:
        value = self._raw(key)
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    def get_money(self, key: str, default: int = 0) -> int:
        """Money settings are stored in Rial, exactly like every other amount."""
        return self.get_int(key, default)

    def as_dict(self) -> dict[str, Any]:
        return {spec.key: self._raw(spec.key) for spec in SPECS.values()}

    # -- writers -----------------------------------------------------------
    async def set(self, session: AsyncSession, key: str, value: Any) -> None:
        if key not in SPECS:
            raise KeyError(f"Unknown setting: {key}")
        spec = SPECS[key]
        coerced = self._coerce(spec, value)
        row = await session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=coerced)  # type: ignore[arg-type]
            session.add(row)
        else:
            row.value = coerced
        await session.flush()
        self._values[key] = coerced
        cache.settings_cache.invalidate(_CACHE_KEY)

    async def set_many(self, session: AsyncSession, values: dict[str, Any]) -> list[str]:
        changed: list[str] = []
        for key, value in values.items():
            if key not in SPECS:
                continue
            await self.set(session, key, value)
            changed.append(key)
        return changed

    async def reset(self, session: AsyncSession, key: str) -> None:
        row = await session.get(Setting, key)
        if row is not None:
            await session.delete(row)
            await session.flush()
        spec = SPECS.get(key)
        if spec:
            self._values[key] = spec.default
        cache.settings_cache.invalidate(_CACHE_KEY)

    @staticmethod
    def _coerce(spec: SettingSpec, value: Any) -> Any:
        if spec.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on", "بله")
        if spec.type in ("int", "money"):
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                number = int(spec.default or 0)
            if spec.minimum is not None:
                number = max(number, int(spec.minimum))
            if spec.maximum is not None:
                number = min(number, int(spec.maximum))
            return number
        if spec.type == "float":
            try:
                number_f = float(value)
            except (TypeError, ValueError):
                number_f = float(spec.default or 0)
            if spec.minimum is not None:
                number_f = max(number_f, spec.minimum)
            if spec.maximum is not None:
                number_f = min(number_f, spec.maximum)
            return number_f
        if spec.type == "choice":
            allowed = {choice for choice, _label in spec.choices}
            text = str(value)
            return text if text in allowed else spec.default
        return "" if value is None else str(value)


#: Process-wide singleton.
app_settings = SettingsStore()


# -- convenience helpers used across the codebase ---------------------------
def shop_name() -> str:
    return app_settings.get_str("shop.name", env.app_name)


def maintenance_mode() -> bool:
    return app_settings.get_bool("shop.maintenance", False)


def card_payments_enabled() -> bool:
    return app_settings.get_bool("payment.card_enabled", True)


def wallet_enabled() -> bool:
    return app_settings.get_bool("payment.wallet_enabled", True)


def test_service_enabled() -> bool:
    return app_settings.get_bool("test.enabled", True)


def button_styles_enabled() -> bool:
    return app_settings.get_bool("appearance.button_styles", True)


def premium_emoji_enabled() -> bool:
    return app_settings.get_bool("appearance.premium_emoji", True)


def reply_keyboard_enabled() -> bool:
    """Should the bot offer the physical (reply) keyboard as well?

    Off by default: the inline menu already covers every screen, and a reply
    keyboard occupies the bottom of the chat on a phone.
    """
    return app_settings.get_bool("appearance.reply_keyboard", False)


def traffic_basis_is_decimal() -> bool:
    """Does one "GB" in this shop mean 1000³ bytes (like the WG-Guard panel)?"""
    return app_settings.get_str("shop.traffic_unit", "gib").strip().lower() == "gb"


def apply_runtime_settings() -> None:
    """Push the stored settings that live in process memory.

    Most settings are read on demand, but the traffic basis is consulted by
    :mod:`app.core.money` on every conversion (including inside the provisioning
    path), so it is cached in a module variable and refreshed here — at startup
    and again whenever the operator saves the settings page.
    """
    from app.core.money import set_gb_basis

    set_gb_basis(decimal=traffic_basis_is_decimal())


__all__ = [
    "GROUP_LABELS",
    "SETTING_KEYS",
    "SPECS",
    "SettingSpec",
    "SettingsStore",
    "app_settings",
    "apply_runtime_settings",
    "button_styles_enabled",
    "card_payments_enabled",
    "maintenance_mode",
    "premium_emoji_enabled",
    "reply_keyboard_enabled",
    "shop_name",
    "spec_groups",
    "test_service_enabled",
    "traffic_basis_is_decimal",
    "wallet_enabled",
]
