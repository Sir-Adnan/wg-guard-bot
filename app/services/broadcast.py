"""Broadcast (پیام همگانی) with throttling and progress.

Telegram rate-limits bulk sending, so the sender walks the audience with a
configurable delay, records progress on every batch and keeps going after a
transient failure.  The task registry lets the panel (or the bot) cancel a
running campaign.

Two delivery shapes share every one of those mechanics:

* a campaign **composed inside the bot** stores the message it came from and is
  delivered with ``copyMessage`` — premium emoji, formatting, media and the
  operator's inline keyboard arrive exactly as typed;
* a campaign created in the panel stores text (and optionally a media
  ``file_id``) and is rendered for the audience: ``{e:key}`` becomes the
  configured premium emoji, ``{shop}`` once per campaign and ``{name}`` per
  recipient.

There is no per-recipient table, so a canceled or interrupted campaign cannot
resume where it stopped — starting it again delivers to the whole audience.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.jalali import now_utc
from app.core.logging import get_logger
from app.core.money import fa_digits
from app.db.models import (
    Broadcast,
    BroadcastStatus,
    Service,
    ServiceStatus,
    Staff,
    User,
)
from app.db.session import session_scope
from app.services.appearance import appearance
from app.services.audit import audit
from app.services.notifications import Media, notifier
from app.services.settings_store import premium_emoji_enabled, shop_name
from app.services.texts import html_escape, texts

log = get_logger(__name__)

#: Audience key → Persian label.  **Order is wire format**: the bot packs the
#: index into its callback payload, so :func:`audience_index` /
#: :func:`audience_key` are the only supported way to move a key across.
AUDIENCES: dict[str, str] = {
    "all": "همه کاربران",
    "active": "کاربران دارای سرویس فعال",
    "expired": "کاربران با سرویس منقضی",
    "no_service": "کاربران بدون سرویس",
    "with_balance": "کاربران دارای موجودی",
}

#: Audience keys in payload order (index 0 is the fallback every screen trusts).
AUDIENCE_KEYS: tuple[str, ...] = tuple(AUDIENCES)

#: What an unknown or out-of-range audience means: everyone.
FALLBACK_AUDIENCE = "all"

#: Telegram tolerates ~30 messages/second overall; stay well under it.
DEFAULT_DELAY = 0.06
PROGRESS_EVERY = 25

#: Keyboard limits for an operator-built inline keyboard.
MAX_BUTTONS = 6
BUTTONS_PER_ROW = 2

#: Only real links travel: a ``javascript:`` or ``tg://`` URL in a mass message
#: is either a mistake or an attack, and Telegram would refuse it anyway.
_URL_PREFIXES = ("http://", "https://")

_EMOJI_PLACEHOLDER_RE = re.compile(r"\{e:([a-z_]+)\}")


# ---------------------------------------------------------------------------
# Audiences
# ---------------------------------------------------------------------------
def audience_index(key: str) -> int:
    """Packed callback value for ``key`` — its index in :data:`AUDIENCE_KEYS`.

    An unknown key falls back to :data:`FALLBACK_AUDIENCE` (index 0) instead of
    whatever happens to live at that position, so a stale button can never
    deliver to a group it does not name.  Packing an unknown key is a caller
    bug; the handler says so instead of sending silently.
    """
    try:
        return AUDIENCE_KEYS.index(key)
    except ValueError:
        log.warning("Unknown broadcast audience %r — using %r", key, FALLBACK_AUDIENCE)
        return 0


def audience_key(index: int) -> str:
    """Audience behind a packed ``index``; out-of-range values mean «all»."""
    if audience_is_known(index):
        return AUDIENCE_KEYS[index]
    log.warning("Unknown broadcast audience index %r — using %r", index, FALLBACK_AUDIENCE)
    return FALLBACK_AUDIENCE


def audience_is_known(index: int) -> bool:
    """``False`` when a callback carried an index this build does not know."""
    return 0 <= index < len(AUDIENCE_KEYS)


def audience_label(key: str) -> str:
    """Persian label for ``key``; an unknown key is shown as it is stored."""
    if key not in AUDIENCES:
        log.warning("Unknown broadcast audience %r — showing the raw key", key)
        return key
    return AUDIENCES[key]


# ---------------------------------------------------------------------------
# Inline keyboard
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class BroadcastButton:
    """One inline-keyboard button of a campaign."""

    text: str
    url: str


def parse_button_rows(text: str) -> list[BroadcastButton]:
    """Parse the panel's ``label | url`` textarea — one button per line.

    Blank lines are skipped so an operator can space rows out visually;
    everything else is returned as typed for :func:`validate_buttons` to judge.
    """
    rows: list[BroadcastButton] = []
    for line in text.splitlines():
        raw = line.strip()
        if not raw:
            continue
        label, _separator, url = raw.partition("|")
        rows.append(BroadcastButton(text=label.strip(), url=url.strip()))
    return rows


def stored_buttons(raw: Any) -> list[BroadcastButton]:
    """Buttons out of a stored JSON ``[{"text": ..., "url": ...}]`` payload."""
    rows: list[BroadcastButton] = []
    for row in raw or []:
        if isinstance(row, dict):
            rows.append(BroadcastButton(text=str(row.get("text") or "").strip(), url=str(row.get("url") or "").strip()))
    return rows


def validate_buttons(rows: list[BroadcastButton]) -> list[BroadcastButton]:
    """Reject a keyboard that Telegram would refuse at send time.

    Called on every creation path, so the operator reads the problem while the
    form is still on screen instead of finding a half-sent campaign later.
    """
    if len(rows) > MAX_BUTTONS:
        raise ValidationError(f"هر پیام حداکثر {fa_digits(MAX_BUTTONS)} دکمه می\u200cتواند داشته باشد.")
    for row in rows:
        if not row.text.strip():
            raise ValidationError("متن دکمه نمی\u200cتواند خالی باشد.")
        if not row.url.startswith(_URL_PREFIXES):
            raise ValidationError("لینک دکمه باید با http:// یا https:// شروع شود.")
    return rows


def buttons_payload(rows: list[BroadcastButton]) -> list[dict[str, str]]:
    """JSON form stored in ``Broadcast.buttons``."""
    return [{"text": row.text, "url": row.url} for row in rows]


async def keyboard_for(raw: Any, session: AsyncSession | None = None) -> InlineKeyboardMarkup | None:
    """Delivery keyboard built from ``Broadcast.buttons``.

    Invalid rows are dropped instead of raising: the campaign is already stored
    and a malformed button must not cost the whole send.  Creation paths run
    :func:`validate_buttons` first, so this leniency is never the operator's
    first warning.
    """
    rows = stored_buttons(raw)
    if not rows:
        return None
    if len(rows) > MAX_BUTTONS:
        log.warning("Broadcast keyboard holds %d buttons — keeping the first %d", len(rows), MAX_BUTTONS)
        rows = rows[:MAX_BUTTONS]

    buttons: list[InlineKeyboardButton] = []
    for row in rows:
        if not row.url.startswith(_URL_PREFIXES):
            log.warning("Dropping broadcast button %r: only http/https links are allowed", row.text[:32])
            continue
        label, icon = await _button_visual(row.text, session)
        if not label:
            log.warning("Dropping broadcast button with an empty label (%r)", row.url[:32])
            continue
        buttons.append(InlineKeyboardButton(text=label, url=row.url, icon_custom_emoji_id=icon))
    if not buttons:
        return None
    rows_of = [buttons[index : index + BUTTONS_PER_ROW] for index in range(0, len(buttons), BUTTONS_PER_ROW)]
    return InlineKeyboardMarkup(inline_keyboard=rows_of)


async def _button_visual(label: str, session: AsyncSession | None = None) -> tuple[str, str | None]:
    """Split ``{e:key}`` out of a button label into a premium-emoji icon.

    Telegram button captions are plain text, so a catalog emoji can only travel
    as ``icon_custom_emoji_id``; without a configured custom id the Unicode
    fallback is prepended, exactly like :class:`~app.bot.keyboards.KeyboardBuilder`.
    """
    match = _EMOJI_PLACEHOLDER_RE.search(label)
    if match is None:
        return label.strip(), None

    clean = _EMOJI_PLACEHOLDER_RE.sub("", label).strip()
    resolved = await appearance.resolve(f"emoji.{match.group(1)}", session)
    if resolved.icon_custom_emoji_id and premium_emoji_enabled():
        return clean or resolved.emoji_fallback, resolved.icon_custom_emoji_id

    fallback = resolved.emoji_fallback
    if not clean:
        return fallback, None
    if not fallback or clean.startswith(fallback):
        return clean, None
    return f"{fallback} {clean}", None


# ---------------------------------------------------------------------------
# Progress + delivery
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class BroadcastProgress:
    broadcast_id: int
    total: int = 0
    sent: int = 0
    failed: int = 0
    done: bool = False


@dataclass(slots=True)
class DeliveryPayload:
    """A campaign in its sendable form, resolved once per run.

    Resolving once matters: appearance/emoji lookups and the keyboard are the
    same for every recipient, and only ``{name}`` needs a per-recipient pass.
    """

    broadcast_id: int
    text: str = ""
    personalized: bool = False
    shop: str = ""
    media: Media | None = None
    source_chat_id: int | None = None
    source_message_id: int | None = None
    keyboard: InlineKeyboardMarkup | None = None

    @property
    def copies_source(self) -> bool:
        """``True`` when the campaign is delivered with ``copyMessage``."""
        return self.source_chat_id is not None and self.source_message_id is not None

    def body(self, name: str = "") -> str:
        """Message body for one recipient (``{name}`` filled only when needed).

        The substituted values are HTML-escaped: a customer may be called
        ``<b>Ali``, and the message is sent as HTML.
        """
        if not self.personalized:
            return self.text
        return texts.format(self.text, name=html_escape(name) or "\u2014", shop=self.shop)


class BroadcastService:
    """Creates and executes broadcast campaigns."""

    def __init__(self) -> None:
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def is_running(self, broadcast_id: int) -> bool:
        task = self._tasks.get(broadcast_id)
        return task is not None and not task.done()

    # -- audience ----------------------------------------------------------
    @staticmethod
    def _audience_query(audience: str):
        """``select(User.telegram_id)`` limited to one audience."""
        if audience not in AUDIENCES:
            raise ValidationError("گروه مخاطبان نامعتبر است.")

        stmt = select(User.telegram_id).where(User.is_blocked.is_(False), User.is_bot_blocked.is_(False))

        if audience == "all":
            pass
        elif audience == "with_balance":
            stmt = stmt.where(User.balance_rial > 0)
        else:
            active_users = select(Service.user_id).where(Service.status == ServiceStatus.ACTIVE)
            expired_users = select(Service.user_id).where(
                Service.status.in_((ServiceStatus.EXPIRED, ServiceStatus.TRAFFIC_EXCEEDED))
            )
            if audience == "active":
                stmt = stmt.where(User.id.in_(active_users))
            elif audience == "expired":
                stmt = stmt.where(User.id.in_(expired_users), User.id.not_in(active_users))
            elif audience == "no_service":
                any_service = select(Service.user_id)
                stmt = stmt.where(User.id.not_in(any_service))
        return stmt

    async def audience_ids(self, session: AsyncSession, audience: str) -> list[int]:
        stmt = self._audience_query(audience)
        return [int(row) for row in (await session.execute(stmt)).scalars()]

    async def preview_count(self, session: AsyncSession, audience: str) -> int:
        return len(await self.audience_ids(session, audience))

    async def audience_names(self, session: AsyncSession, telegram_ids: list[int]) -> dict[int, str]:
        """Display names for ``{name}`` — one query, only when it is needed."""
        if not telegram_ids:
            return {}
        rows = (await session.execute(select(User).where(User.telegram_id.in_(telegram_ids)))).scalars()
        return {int(user.telegram_id): user.display_name for user in rows}

    # -- creation ----------------------------------------------------------
    async def create(
        self,
        session: AsyncSession,
        staff: Staff | None,
        *,
        text: str,
        audience: str = "all",
        media: Media | None = None,
        buttons: list[dict] | None = None,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
        start_immediately: bool = True,
    ) -> Broadcast:
        if not text.strip():
            raise ValidationError("متن پیام نمی\u200cتواند خالی باشد.")
        if audience not in AUDIENCES:
            raise ValidationError("گروه مخاطبان نامعتبر است.")
        if buttons:
            validate_buttons(stored_buttons(buttons))

        broadcast = Broadcast(
            created_by_staff_id=staff.id if staff else None,
            text=text,
            audience=audience,
            media_file_id=media.file_id if media else None,
            media_type=media.kind if media else None,
            buttons=buttons or None,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            status=BroadcastStatus.DRAFT,
            total=await self.preview_count(session, audience),
        )
        session.add(broadcast)
        await session.flush()

        await audit.record(
            session,
            "broadcast.create",
            actor=staff,
            entity="broadcast",
            entity_id=broadcast.id,
            description=f"audience={audience} total={broadcast.total}",
        )
        if start_immediately:
            await session.flush()
        return broadcast

    # -- execution ---------------------------------------------------------
    async def start(self, broadcast_id: int) -> bool:
        """Kick off a campaign in the background (``False`` when already running)."""
        if not notifier.bound:
            raise ValidationError("ربات تلگرام فعال نیست، بنابراین ارسال همگانی ممکن نیست. BOT_TOKEN را بررسی کنید.")
        if self.is_running(broadcast_id):
            return False
        task = asyncio.create_task(self._run(broadcast_id), name=f"broadcast-{broadcast_id}")
        self._tasks[broadcast_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(broadcast_id, None))
        return True

    async def cancel(self, broadcast_id: int) -> bool:
        task = self._tasks.get(broadcast_id)
        if task is not None and not task.done():
            task.cancel()
        async with session_scope() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return False
            if broadcast.status == BroadcastStatus.RUNNING:
                broadcast.status = BroadcastStatus.CANCELED
                broadcast.finished_at = now_utc()
        return True

    async def prepare(self, session: AsyncSession, broadcast: Broadcast) -> DeliveryPayload:
        """Resolve a stored campaign into everything the loop needs."""
        rendered = await appearance.render_text(broadcast.text or "", session)
        personal = "{name}" in rendered
        shop = html_escape(shop_name())
        media = (
            Media(kind=broadcast.media_type, file_id=broadcast.media_file_id)  # type: ignore[arg-type]
            if broadcast.media_type and broadcast.media_file_id
            else None
        )
        return DeliveryPayload(
            broadcast_id=broadcast.id,
            text=rendered if personal else texts.format(rendered, shop=shop),
            personalized=personal,
            shop=shop,
            media=media,
            source_chat_id=broadcast.source_chat_id,
            source_message_id=broadcast.source_message_id,
            keyboard=await keyboard_for(broadcast.buttons, session),
        )

    async def deliver_one(
        self, session: AsyncSession, broadcast: Broadcast, telegram_id: int, *, name: str = ""
    ) -> bool:
        """Send a single copy outside the campaign — the «ارسال آزمایشی» button."""
        return await self._deliver(await self.prepare(session, broadcast), telegram_id, name=name)

    async def _deliver(self, payload: DeliveryPayload, telegram_id: int, *, name: str = "") -> bool:
        """Deliver one copy; ``True`` when Telegram accepted it.

        A composed campaign is copied verbatim, which is the only way premium
        emoji and the operator's formatting survive.  Everything else goes
        through the text/media path with the placeholders resolved.
        """
        if payload.copies_source:
            message = await notifier.copy(
                telegram_id,
                int(payload.source_chat_id or 0),
                int(payload.source_message_id or 0),
                keyboard=payload.keyboard,
            )
        else:
            message = await notifier.send(
                telegram_id,
                payload.body(name),
                media=payload.media,
                keyboard=payload.keyboard,
            )
        return message is not None

    async def _run(self, broadcast_id: int) -> None:
        async with session_scope() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return
            if broadcast.status == BroadcastStatus.DONE:
                return
            if broadcast.status == BroadcastStatus.RUNNING:
                # ``start`` never lets two tasks share an id, so a RUNNING row
                # here is a campaign whose process died mid-send.  There is no
                # per-recipient table to resume from: the only recovery is a
                # fresh run, and the panel labels the button accordingly.
                log.warning("Broadcast %s was left running — starting it over", broadcast_id)
            broadcast.status = BroadcastStatus.RUNNING
            broadcast.started_at = broadcast.started_at or now_utc()
            payload = await self.prepare(session, broadcast)
            targets = await self.audience_ids(session, broadcast.audience)
            names = await self.audience_names(session, targets) if payload.personalized else {}
            broadcast.total = len(targets)

        sent = 0
        failed = 0
        try:
            for index, telegram_id in enumerate(targets, start=1):
                delivered = await self._deliver(payload, telegram_id, name=names.get(telegram_id, ""))
                if delivered:
                    sent += 1
                else:
                    failed += 1
                if index % PROGRESS_EVERY == 0:
                    await self._save_progress(broadcast_id, sent, failed)
                await asyncio.sleep(DEFAULT_DELAY)
        except asyncio.CancelledError:
            await self._save_progress(broadcast_id, sent, failed, status=BroadcastStatus.CANCELED)
            log.info("Broadcast %s canceled after %d sends", broadcast_id, sent)
            raise
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Broadcast %s crashed", broadcast_id)
            await self._save_progress(broadcast_id, sent, failed, status=BroadcastStatus.FAILED)
            await notifier.report_error(exc, source="broadcast")
            return

        await self._save_progress(broadcast_id, sent, failed, status=BroadcastStatus.DONE)
        log.info("Broadcast %s finished: %d sent / %d failed", broadcast_id, sent, failed)

    async def _save_progress(
        self,
        broadcast_id: int,
        sent: int,
        failed: int,
        *,
        status: BroadcastStatus | None = None,
    ) -> None:
        async with session_scope() as session:
            broadcast = await session.get(Broadcast, broadcast_id)
            if broadcast is None:
                return
            broadcast.sent = sent
            broadcast.failed = failed
            if status is not None:
                broadcast.status = status
                broadcast.finished_at = (
                    now_utc()
                    if status
                    in (
                        BroadcastStatus.DONE,
                        BroadcastStatus.CANCELED,
                        BroadcastStatus.FAILED,
                    )
                    else None
                )

    async def shutdown(self) -> None:
        for task in list(self._tasks.values()):
            if not task.done():
                task.cancel()
        self._tasks.clear()

    # -- queries -----------------------------------------------------------
    async def list_recent(self, session: AsyncSession, *, limit: int = 25) -> list[Broadcast]:
        return list(
            (await session.execute(select(Broadcast).order_by(Broadcast.created_at.desc()).limit(limit))).scalars()
        )

    async def get(self, session: AsyncSession, broadcast_id: int) -> Broadcast:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None:
            raise NotFoundError("ارسال همگانی مورد نظر پیدا نشد.")
        return broadcast


broadcasts = BroadcastService()


__all__ = [
    "AUDIENCES",
    "AUDIENCE_KEYS",
    "BUTTONS_PER_ROW",
    "FALLBACK_AUDIENCE",
    "MAX_BUTTONS",
    "BroadcastButton",
    "BroadcastProgress",
    "BroadcastService",
    "DeliveryPayload",
    "audience_index",
    "audience_is_known",
    "audience_key",
    "audience_label",
    "broadcasts",
    "buttons_payload",
    "keyboard_for",
    "parse_button_rows",
    "stored_buttons",
    "validate_buttons",
]
