"""Mandatory channel membership ("عضویت اجباری").

The check is cached for ``membership.recheck_minutes`` so a user tapping around
the menu does not generate a ``getChatMember`` call per button press.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import cache
from app.core.logging import get_logger
from app.db.models import Channel
from app.services.notifications import notifier
from app.services.settings_store import app_settings
from app.services.texts import texts

log = get_logger(__name__)

#: Chat-member statuses that count as "joined".
MEMBER_STATUSES = {"creator", "administrator", "member", "restricted"}


@dataclass(slots=True)
class MembershipStatus:
    ok: bool
    missing: list[Channel]

    @property
    def missing_titles(self) -> str:
        return "، ".join(channel.title or channel.chat_id for channel in self.missing)


class MembershipService:
    """Reads channels, checks membership, builds the join keyboard."""

    # -- channels ----------------------------------------------------------
    async def active_channels(self, session: AsyncSession) -> list[Channel]:
        return list(
            (
                await session.execute(
                    select(Channel)
                    .where(Channel.is_active.is_(True))
                    .order_by(Channel.sort_order.asc(), Channel.id.asc())
                )
            ).scalars()
        )

    async def all_channels(self, session: AsyncSession) -> list[Channel]:
        return list(
            (await session.execute(select(Channel).order_by(Channel.sort_order.asc(), Channel.id.asc()))).scalars()
        )

    def enabled(self) -> bool:
        return app_settings.get_bool("membership.enabled", False)

    # -- checking ----------------------------------------------------------
    async def check(self, session: AsyncSession, telegram_id: int, *, force: bool = False) -> MembershipStatus:
        """Return which required channels the user has not joined yet."""
        if not self.enabled():
            return MembershipStatus(ok=True, missing=[])

        channels = await self.active_channels(session)
        if not channels:
            return MembershipStatus(ok=True, missing=[])

        ttl = max(app_settings.get_int("membership.recheck_minutes", 30), 1) * 60
        cache_key = f"member:{telegram_id}"
        if not force:
            cached = await cache.misc_cache.get(cache_key)
            if cached is not None:
                missing_ids = set(cached)
                return MembershipStatus(
                    ok=not missing_ids,
                    missing=[c for c in channels if c.id in missing_ids],
                )

        missing: list[Channel] = []
        for channel in channels:
            joined = await self._is_member(channel.chat_id, telegram_id)
            if not joined:
                missing.append(channel)

        await cache.misc_cache.set(cache_key, [c.id for c in missing], ttl=ttl)
        return MembershipStatus(ok=not missing, missing=missing)

    async def _is_member(self, chat_id: str, telegram_id: int) -> bool:
        """One ``getChatMember`` call; unknown chat ids count as "joined" so a
        mis-configured channel can never lock every customer out."""
        if not notifier.bound:
            return True
        target: str | int = chat_id
        if chat_id.lstrip("-").isdigit():
            target = int(chat_id)
        try:
            member = await notifier.bot.get_chat_member(chat_id=target, user_id=telegram_id)
        except TelegramBadRequest as exc:
            text = str(exc).lower()
            if "chat not found" in text or "member list is inaccessible" in text:
                await notifier.record_event(
                    "warning",
                    f"بررسی عضویت برای {chat_id} ممکن نیست؛ ربات را در کانال ادمین کنید.",
                    source="membership",
                )
                return True
            log.debug("get_chat_member(%s, %s) failed: %s", chat_id, telegram_id, exc)
            return True
        except TelegramForbiddenError:
            return True
        except Exception as exc:  # pragma: no cover - network
            log.debug("membership check error: %s", exc)
            return True
        return getattr(member, "status", "") in MEMBER_STATUSES

    async def invalidate(self, telegram_id: int) -> None:
        cache.misc_cache.invalidate(f"member:{telegram_id}")

    async def invalidate_all(self) -> None:
        cache.misc_cache.invalidate_prefix("member:")

    # -- presentation ------------------------------------------------------
    async def join_keyboard(self, session: AsyncSession, missing: list[Channel]):
        from app.bot.callbacks import MenuCB
        from app.bot.keyboards import KeyboardBuilder

        kb = KeyboardBuilder(session=session)
        for channel in missing:
            if channel.invite_link:
                await kb.add("menu.join", url=channel.invite_link, text=channel.title or None)
        await kb.add("menu.check_join", callback=MenuCB(action="check_join").pack())
        return kb.build()

    async def join_prompt(self, session: AsyncSession, missing: list[Channel]) -> str:
        listed = "\n".join(f"• {c.title or c.chat_id}" for c in missing)
        return await texts.get("start.must_join", session, channels="\n" + listed + "\n")

    # -- join requests -----------------------------------------------------
    async def maybe_auto_approve(self, session: AsyncSession, chat_id: int, user_id: int) -> bool:
        """Approve a pending join request when the channel is configured for it."""
        channel = (
            await session.execute(
                select(Channel).where(
                    Channel.is_active.is_(True),
                    Channel.auto_approve_joins.is_(True),
                )
            )
        ).scalars()
        targets = {c.chat_id for c in channel}
        key = str(chat_id)
        if key not in targets and f"-100{chat_id}" not in targets:
            return False
        if not notifier.bound:
            return False
        try:
            await notifier.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
        except Exception as exc:
            log.debug("approve_chat_join_request failed: %s", exc)
            return False
        await self.invalidate(user_id)
        return True


membership = MembershipService()


__all__ = ["MEMBER_STATUSES", "MembershipService", "MembershipStatus", "membership"]
