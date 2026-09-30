"""Outbound messaging.

Every message the bot sends goes through :class:`Notifier`.  Centralising it
buys us four things that would otherwise be duplicated across ~20 handlers:

1. **Graceful styling fallback** — if Telegram (or an older self-hosted Bot API
   server) rejects ``style`` / ``icon_custom_emoji_id``, the message is resent
   with the plain keyboard twin and styling is switched off process-wide.
2. **Blocked-user tracking** — ``TelegramForbiddenError`` flips
   ``user.is_bot_blocked`` instead of spamming the log.
3. **Staff fan-out** — one call delivers to every reviewer and returns the
   ``(chat_id, message_id)`` pairs needed to edit them all later.
4. **System events** — failures are recorded in ``system_events`` so the panel
   can show them without anyone reading container logs.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramUnauthorizedError,
)
from aiogram.types import InlineKeyboardMarkup, LinkPreviewOptions, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import EventLevel, Staff, StaffRole, SystemEvent, User

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a services->bot import cycle
    pass

log = get_logger(__name__)

#: A raw markup, a styled :class:`~app.bot.keyboards.KB`, or nothing.
#: Duck-typed on purpose: the services layer must not import the bot layer.
KeyboardLike = Any

#: Substrings that identify a rejection caused by the new styling fields.
_STYLE_ERROR_HINTS = ("style", "custom emoji", "icon_custom_emoji", "BUTTON_")

#: Set once so an unbound bot warns a single time instead of on every message.
_UNBOUND_WARNED = False


@dataclass(slots=True)
class Media:
    """Optional attachment for an outgoing message."""

    kind: Literal["photo", "document", "video", "animation"]
    file_id: str

    def method_kwargs(self) -> dict[str, Any]:
        field = {
            "photo": "photo",
            "document": "document",
            "video": "video",
            "animation": "animation",
        }[self.kind]
        return {field: self.file_id}


class Notifier:
    """Bot-aware sender.  Bound once at startup via :meth:`bind`."""

    def __init__(self) -> None:
        self._bot: Bot | None = None
        self._styles_ok = True
        self._emoji_ok = True

    # -- lifecycle ---------------------------------------------------------
    def bind(self, bot: Bot) -> None:
        self._bot = bot

    @property
    def bound(self) -> bool:
        return self._bot is not None

    @property
    def username(self) -> str | None:
        """Bot username when known (used to build referral/deep links)."""
        if self._bot is None:
            return None
        user = getattr(self._bot, "_me", None)
        return getattr(user, "username", None) if user is not None else None

    @property
    def bot(self) -> Bot:
        if self._bot is None:
            raise RuntimeError("Notifier.bind(bot) has not been called yet")
        return self._bot

    @property
    def styling_supported(self) -> bool:
        return self._styles_ok or self._emoji_ok

    def disable_styling(self) -> None:
        if self._styles_ok or self._emoji_ok:
            log.warning(
                "Telegram rejected button styling — falling back to plain keyboards. "
                "Update your Bot API server or disable styling in the admin panel."
            )
        self._styles_ok = False
        self._emoji_ok = False

    # -- low level ---------------------------------------------------------
    def _unbound(self, what: str) -> None:
        """Log (once per process) that the bot is not running.

        The admin panel must stay fully usable when ``BOT_TOKEN`` is unset or
        Telegram is unreachable: the operator can still review receipts, build
        services and answer tickets.  Outbound messages are simply dropped.
        """
        global _UNBOUND_WARNED
        if not _UNBOUND_WARNED:
            log.warning(
                "Telegram bot is not bound — outbound messages are skipped (%s). "
                "Set BOT_TOKEN and restart to enable delivery.",
                what,
            )
            _UNBOUND_WARNED = True

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        keyboard: KeyboardLike = None,
        media: Media | None = None,
        disable_preview: bool = True,
        **kwargs: Any,
    ) -> Message | None:
        """Send one message, retrying without styling when necessary."""
        if self._bot is None:
            self._unbound("send")
            return None

        markup, plain_markup = self._markups(keyboard)

        send_kwargs: dict[str, Any] = {"chat_id": chat_id, "reply_markup": markup, **kwargs}
        if media is None:
            send_kwargs["text"] = text
            if disable_preview:
                # ``link_preview_options`` exists on ``sendMessage`` only: passing
                # it to ``sendPhoto``/``sendVideo``/… raises ``TypeError`` before
                # a single byte leaves the process, so a receipt photo would never
                # reach a reviewer.  Captions have no link preview anyway.
                send_kwargs["link_preview_options"] = LinkPreviewOptions(is_disabled=True)
            method = self.bot.send_message
        else:
            if media.kind == "photo":
                send_kwargs["caption"] = text
                method = self.bot.send_photo
            elif media.kind == "video":
                send_kwargs["caption"] = text
                method = self.bot.send_video
            elif media.kind == "animation":
                send_kwargs["caption"] = text
                method = self.bot.send_animation
            else:
                send_kwargs["caption"] = text
                method = self.bot.send_document
            send_kwargs.update(media.method_kwargs())

        try:
            return await method(**send_kwargs)  # type: ignore[operator]
        except TelegramRetryAfter as exc:
            import asyncio

            log.warning("Flood control: retrying in %ss", exc.retry_after)
            await asyncio.sleep(exc.retry_after + 1)
            return await self.send(
                chat_id, text, keyboard=keyboard, media=media, disable_preview=disable_preview, **kwargs
            )
        except TelegramForbiddenError:
            log.info("Chat %s has blocked the bot", chat_id)
            await self._mark_blocked(chat_id)
            return None
        except TelegramUnauthorizedError:
            log.critical("Bot token is invalid — the bot cannot send messages")
            raise
        except TelegramBadRequest as exc:
            if plain_markup is not None and self._is_style_error(exc):
                self.disable_styling()
                try:
                    retry_kwargs = dict(send_kwargs)
                    retry_kwargs["reply_markup"] = plain_markup
                    return await method(**retry_kwargs)  # type: ignore[operator]
                except Exception as retry_exc:  # pragma: no cover
                    log.error("Plain-keyboard retry failed: %s", retry_exc)
                    return None
            log.warning("send to %s failed: %s", chat_id, exc)
            return None
        except TelegramNetworkError as exc:
            log.warning("Network error sending to %s: %s", chat_id, exc)
            return None

    async def edit(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        keyboard: KeyboardLike = None,
        media: Media | None = None,
    ) -> bool:
        """Edit a previously sent message; returns ``True`` on success."""
        if self._bot is None:
            return False
        markup, plain_markup = self._markups(keyboard)
        try:
            if media is None:
                await self.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
            else:
                from aiogram.types import InputMediaAnimation, InputMediaDocument, InputMediaPhoto, InputMediaVideo

                classes = {
                    "photo": InputMediaPhoto,
                    "document": InputMediaDocument,
                    "video": InputMediaVideo,
                    "animation": InputMediaAnimation,
                }
                await self.bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=message_id,
                    media=classes[media.kind](media=media.file_id, caption=text),
                    reply_markup=markup,
                )
            return True
        except TelegramBadRequest as exc:
            if plain_markup is not None and self._is_style_error(exc):
                self.disable_styling()
                return await self.edit(chat_id, message_id, text, keyboard=plain_markup, media=media)
            # "message is not modified" and deleted messages are both benign here.
            if "not modified" not in str(exc):
                log.debug("edit %s/%s failed: %s", chat_id, message_id, exc)
            return False
        except TelegramForbiddenError:
            return False

    async def send_upload(
        self,
        chat_id: int,
        content: bytes,
        filename: str,
        *,
        caption: str | None = None,
        as_photo: bool = False,
        keyboard: KeyboardLike = None,
    ) -> Message | None:
        """Send an in-memory file (config, QR image) with the usual error handling."""
        from aiogram.types import BufferedInputFile

        if self._bot is None:
            self._unbound("send_upload")
            return None

        markup, _ = self._markups(keyboard)
        upload = BufferedInputFile(content, filename=filename)
        try:
            if as_photo:
                return await self.bot.send_photo(chat_id=chat_id, photo=upload, caption=caption, reply_markup=markup)
            return await self.bot.send_document(chat_id=chat_id, document=upload, caption=caption, reply_markup=markup)
        except TelegramForbiddenError:
            await self._mark_blocked(chat_id)
            return None
        except TelegramBadRequest as exc:
            log.warning("upload to %s failed: %s", chat_id, exc)
            return None
        except TelegramNetworkError as exc:
            log.warning("network error uploading to %s: %s", chat_id, exc)
            return None

    async def edit_caption(
        self,
        chat_id: int,
        message_id: int,
        caption: str,
        *,
        keyboard: KeyboardLike = None,
        clear_keyboard: bool = False,
    ) -> bool:
        """Edit the caption of a media message (keeps the attachment in place)."""
        if self._bot is None:
            return False
        markup, plain_markup = self._markups(keyboard)
        if clear_keyboard:
            markup = InlineKeyboardMarkup(inline_keyboard=[])
            plain_markup = None
        try:
            await self.bot.edit_message_caption(
                chat_id=chat_id, message_id=message_id, caption=caption, reply_markup=markup
            )
            return True
        except TelegramBadRequest as exc:
            if plain_markup is not None and self._is_style_error(exc):
                self.disable_styling()
                return await self.edit_caption(
                    chat_id, message_id, caption, keyboard=plain_markup, clear_keyboard=clear_keyboard
                )
            if "not modified" not in str(exc):
                log.debug("edit_caption %s/%s failed: %s", chat_id, message_id, exc)
            return False
        except TelegramForbiddenError:
            return False

    def _markups(self, keyboard: KeyboardLike) -> tuple[InlineKeyboardMarkup | None, InlineKeyboardMarkup | None]:
        if keyboard is None:
            return None, None
        # A styled KB exposes ``markup`` + ``plain()``; a raw markup does not.
        if hasattr(keyboard, "markup") and hasattr(keyboard, "plain"):
            if not self.styling_supported:
                return keyboard.plain() or keyboard.markup, None
            return keyboard.markup, keyboard.plain()
        return keyboard, None

    @staticmethod
    def _is_style_error(exc: TelegramBadRequest) -> bool:
        text = str(exc).lower()
        return any(hint.lower() in text for hint in _STYLE_ERROR_HINTS)

    async def _mark_blocked(self, chat_id: int) -> None:
        from app.db.session import session_scope

        try:
            async with session_scope() as session:
                user = (await session.execute(select(User).where(User.telegram_id == chat_id))).scalar_one_or_none()
                if user is not None and not user.is_bot_blocked:
                    user.is_bot_blocked = True
        except Exception as exc:  # pragma: no cover - never break on bookkeeping
            log.debug("Could not flag blocked user %s: %s", chat_id, exc)

    # -- high level --------------------------------------------------------
    async def to_user(
        self,
        user: User | int,
        text: str,
        *,
        keyboard: KeyboardLike = None,
        media: Media | None = None,
        **kwargs: Any,
    ) -> Message | None:
        chat_id = user.telegram_id if isinstance(user, User) else int(user)
        message = await self.send(chat_id, text, keyboard=keyboard, media=media, **kwargs)
        if message is not None and isinstance(user, User) and user.is_bot_blocked:
            user.is_bot_blocked = False
        return message

    async def to_staff(
        self,
        session: AsyncSession,
        text: str,
        *,
        roles: Sequence[StaffRole] = (StaffRole.OWNER, StaffRole.ADMIN, StaffRole.SUPPORT),
        reviewers_only: bool = False,
        keyboard: KeyboardLike = None,
        media: Media | None = None,
        exclude: Iterable[int] = (),
        **kwargs: Any,
    ) -> list[tuple[int, int]]:
        """Deliver to every active operator; returns ``(chat_id, message_id)``."""
        stmt = select(Staff).where(Staff.is_active.is_(True), Staff.role.in_(list(roles)))
        if reviewers_only:
            stmt = stmt.where(Staff.receive_receipts.is_(True))
        staff_rows = list((await session.execute(stmt)).scalars())

        skip = set(exclude)
        delivered: list[tuple[int, int]] = []
        for member in staff_rows:
            if member.telegram_id is None or member.telegram_id in skip:
                continue
            message = await self.send(member.telegram_id, text, keyboard=keyboard, media=media, **kwargs)
            if message is not None:
                delivered.append((member.telegram_id, message.message_id))
        return delivered

    async def to_admins(
        self, session: AsyncSession, text: str, *, keyboard: KeyboardLike = None, **kwargs: Any
    ) -> list[tuple[int, int]]:
        return await self.to_staff(session, text, roles=(StaffRole.OWNER, StaffRole.ADMIN), keyboard=keyboard, **kwargs)

    async def to_log_channel(self, session: AsyncSession, text: str) -> None:
        from app.core.money import en_digits
        from app.services.settings_store import app_settings

        raw = app_settings.get_str("advanced.log_channel_id", "").strip()
        if not raw:
            return
        try:
            chat_id = int(en_digits(raw))
        except ValueError:
            return
        await self.send(chat_id, text)

    # -- system events -----------------------------------------------------
    async def record_event(
        self,
        level: EventLevel,
        message: str,
        *,
        source: str = "app",
        meta: dict[str, Any] | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        """Persist an event for the panel, optionally notifying admins."""
        payload = SystemEvent(level=level, source=source, message=message[:4000], meta=meta or {})
        try:
            if session is not None:
                session.add(payload)
                await session.flush()
            else:
                from app.db.session import session_scope

                async with session_scope() as own_session:
                    own_session.add(payload)
        except Exception as exc:  # pragma: no cover
            log.debug("Could not record system event: %s", exc)
            return

        log.log(
            {"info": 20, "warning": 30, "error": 40, "critical": 50}[level.value],
            "[%s] %s",
            source,
            message,
        )

    async def report_error(self, exc: BaseException, *, source: str, notify: bool = True) -> None:
        """Record an exception and (optionally) alert the operators."""
        level = EventLevel.ERROR
        text = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, AppError):
            text = exc.message
            meta = {"code": exc.code, **exc.details}
        else:
            meta = {}
        await self.record_event(level, text, source=source, meta=meta)

        if not notify:
            return
        from app.db.session import session_scope
        from app.services.settings_store import app_settings

        if not app_settings.get_bool("notify.admin_panel_errors", True):
            return
        try:
            async with session_scope() as session:
                await self.to_admins(
                    session,
                    f"🚨 <b>خطای سیستم</b>\n\n<b>منبع:</b> <code>{source}</code>\n<b>توضیح:</b> {_escape(text)[:800]}",
                    disable_notification=False,
                )
        except Exception:  # pragma: no cover - alerting must never raise
            pass


def _escape(value: Any) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


#: Process-wide singleton.
notifier = Notifier()


__all__ = ["Media", "Notifier", "notifier"]
