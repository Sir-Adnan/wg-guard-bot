"""Guides / tutorials ("آموزش اتصال" and the FAQ)."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.callbacks import GuideCB, MenuCB
from app.bot.menus import guide_list, guide_sections
from app.bot.utils import answer_callback, show
from app.core.errors import AppError
from app.core.logging import get_logger
from app.db.models import User
from app.services.guides import PLATFORM_LABELS, SECTION_LABELS, guides
from app.services.notifications import Media, notifier
from app.services.texts import html_escape, texts

log = get_logger(__name__)
router = Router(name="guides")


@router.callback_query(MenuCB.filter(F.action == "guides"))
async def open_guides(callback: CallbackQuery, session: AsyncSession, user: User) -> None:
    await answer_callback(callback)
    await _render_sections(callback, session)


@router.callback_query(GuideCB.filter(F.action == "section"))
async def open_section(callback: CallbackQuery, callback_data: GuideCB, session: AsyncSession) -> None:
    await answer_callback(callback)
    section = callback_data.section
    if not section or section == "__root__":
        await _render_sections(callback, session)
        return

    items = await guides.list_section(session, section)
    if not items:
        await _render_sections(callback, session)
        return

    label = SECTION_LABELS.get(section, section)
    body = f"📚 <b>{label}</b>\n\nیکی از موارد زیر را انتخاب کنید:"
    await show(callback, body, keyboard=await guide_list(session, items, section))


@router.callback_query(GuideCB.filter(F.action == "read"))
async def read_guide(callback: CallbackQuery, callback_data: GuideCB, session: AsyncSession, user: User) -> None:
    try:
        guide = await guides.get(session, callback_data.guide_id)
    except AppError:
        await callback.answer("این مطلب پیدا نشد.", show_alert=True)
        return

    await answer_callback(callback)
    await guides.bump_views(session, guide.id)

    platform = PLATFORM_LABELS.get(guide.platform, "")
    header = f"<b>{html_escape(guide.title)}</b>"
    if platform and guide.platform != "all":
        header += f"\n<i>مخصوص {platform}</i>"
    body = f"{header}\n\n{guide.body}" if guide.body else header

    backup = await texts.get("error.not_found", session)
    if guide.media_file_id:
        media = Media(
            kind="photo" if guide.media_type == "photo" else "document",
            file_id=guide.media_file_id,
        )
        sent = await notifier.to_user(user, body, media=media)
        if sent is None:
            await notifier.to_user(user, body)
    else:
        await show(callback, body or backup)

    # Keep the reader inside the section.
    items = await guides.list_section(session, guide.section)
    if items:
        from app.bot.keyboards import KeyboardBuilder

        kb = KeyboardBuilder(session=session, columns=1)
        for item in items[:8]:
            await kb.add(
                "guide.read",
                text=("✅ " if item.id == guide.id else "") + item.title[:56],
                callback=GuideCB(action="read", guide_id=item.id).pack(),
            )
        kb.row()
        await kb.add("menu.back", callback=GuideCB(action="section", section="__root__").pack())
        await notifier.to_user(user, "🔽 ادامه مطالب:", keyboard=kb.build())


async def _render_sections(callback: CallbackQuery, session: AsyncSession) -> None:
    counts = await guides.sections(session)
    if not counts:
        await show(callback, await texts.get("guides.empty", session))
        return
    await show(
        callback,
        await texts.get("guides.title", session),
        keyboard=await guide_sections(session, counts),
    )


__all__ = ["router"]
