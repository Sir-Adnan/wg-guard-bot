"""Connection guides and FAQ articles.

Iranian VPN customers ask the same three questions every day ("چه برنامه‌ای
نصب کنم؟"، "چرا وصل نمی‌شود؟"، "کانفیگ را چطور وارد کنم؟").  Answering them
inside the bot cuts support load dramatically, so guides are first-class content
with their own panel page.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError
from app.core.logging import get_logger
from app.db.models import Guide

log = get_logger(__name__)

SECTION_LABELS: dict[str, str] = {
    "connect": "آموزش اتصال",
    "buy": "راهنمای خرید",
    "faq": "سؤالات پرتکرار",
    "other": "سایر",
}

PLATFORM_LABELS: dict[str, str] = {
    "all": "همه دستگاه‌ها",
    "android": "اندروید",
    "ios": "آیفون و آیپد",
    "windows": "ویندوز",
    "macos": "مک",
    "linux": "لینوکس",
}


class GuideService:
    """CRUD plus the ordered listings the bot renders."""

    async def get(self, session: AsyncSession, guide_id: int) -> Guide:
        guide = await session.get(Guide, guide_id)
        if guide is None:
            raise NotFoundError("مطلب مورد نظر پیدا نشد.")
        return guide

    async def list_section(
        self, session: AsyncSession, section: str, *, platform: str | None = None, active_only: bool = True
    ) -> list[Guide]:
        stmt = select(Guide).where(Guide.section == section)
        if active_only:
            stmt = stmt.where(Guide.is_active.is_(True))
        if platform:
            stmt = stmt.where(Guide.platform.in_((platform, "all")))
        stmt = stmt.order_by(Guide.sort_order.asc(), Guide.id.asc())
        return list((await session.execute(stmt)).scalars())

    async def all(self, session: AsyncSession, *, limit: int = 200) -> list[Guide]:
        return list(
            (
                await session.execute(
                    select(Guide).order_by(Guide.section.asc(), Guide.sort_order.asc(), Guide.id.asc()).limit(limit)
                )
            ).scalars()
        )

    async def sections(self, session: AsyncSession) -> dict[str, int]:
        rows = await session.execute(select(Guide.section, func.count(Guide.id)).group_by(Guide.section))
        return {section: int(count) for section, count in rows.all()}

    async def count(self, session: AsyncSession, *, active_only: bool = True) -> int:
        stmt = select(func.count(Guide.id))
        if active_only:
            stmt = stmt.where(Guide.is_active.is_(True))
        return int(await session.scalar(stmt) or 0)

    async def create(
        self,
        session: AsyncSession,
        *,
        title: str,
        section: str = "connect",
        platform: str = "all",
        body: str = "",
        media_file_id: str | None = None,
        media_type: str | None = None,
        sort_order: int = 0,
        is_active: bool = True,
    ) -> Guide:
        clean = title.strip()
        if not clean:
            raise ValidationError("عنوان مطلب نمی‌تواند خالی باشد.")
        if section not in SECTION_LABELS:
            raise ValidationError("بخش انتخابی نامعتبر است.")
        if platform not in PLATFORM_LABELS:
            raise ValidationError("پلتفرم انتخابی نامعتبر است.")

        guide = Guide(
            title=clean[:160],
            section=section,
            platform=platform,
            body=body,
            media_file_id=media_file_id,
            media_type=media_type,
            sort_order=sort_order,
            is_active=is_active,
        )
        session.add(guide)
        await session.flush()
        log.info("Guide created: %s [%s]", guide.title, section)
        return guide

    async def update(self, session: AsyncSession, guide_id: int, **fields) -> Guide:
        guide = await self.get(session, guide_id)
        for key in ("title", "section", "platform", "body", "media_file_id", "media_type", "sort_order", "is_active"):
            if key in fields and fields[key] is not None:
                setattr(guide, key, fields[key])
        await session.flush()
        return guide

    async def delete(self, session: AsyncSession, guide_id: int) -> None:
        guide = await self.get(session, guide_id)
        await session.delete(guide)
        await session.flush()

    async def bump_views(self, session: AsyncSession, guide_id: int) -> None:
        guide = await session.get(Guide, guide_id)
        if guide is not None:
            guide.views = int(guide.views) + 1
            await session.flush()


guides = GuideService()


__all__ = ["PLATFORM_LABELS", "SECTION_LABELS", "GuideService", "guides"]
