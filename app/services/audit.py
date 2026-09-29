"""Audit trail helper.

Short, explicit and dependency-free so any service can record *who changed
what* without pulling in the web layer.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models import AuditLog

log = get_logger(__name__)


class AuditService:
    """Appends rows to ``audit_logs``."""

    @staticmethod
    async def record(
        session: AsyncSession,
        action: str,
        *,
        actor: Any = None,
        user_id: int | None = None,
        entity: str | None = None,
        entity_id: str | int | None = None,
        description: str | None = None,
        meta: dict[str, Any] | None = None,
        ip: str | None = None,
        flush: bool = True,
    ) -> AuditLog:
        """Write one audit entry.

        ``actor`` may be a :class:`~app.db.models.Staff` instance, a Telegram
        id, or a free-form label such as ``"telegram:12345"``.
        """
        staff_id: int | None = None
        label: str | None = None

        if actor is not None:
            if hasattr(actor, "id") and hasattr(actor, "role"):
                staff_id = int(actor.id)
                label = f"{getattr(actor, 'name', '') or 'staff'}#{staff_id}"
            elif isinstance(actor, int):
                label = f"telegram:{actor}"
            else:
                label = str(actor)

        entry = AuditLog(
            staff_id=staff_id,
            user_id=user_id,
            actor_label=label,
            action=action,
            entity=entity,
            entity_id=str(entity_id) if entity_id is not None else None,
            description=description,
            meta=meta or {},
            ip=ip,
        )
        session.add(entry)
        if flush:
            await session.flush()
        log.debug("audit: %s %s/%s by %s", action, entity, entity_id, label)
        return entry


audit = AuditService()


__all__ = ["AuditService", "audit"]
