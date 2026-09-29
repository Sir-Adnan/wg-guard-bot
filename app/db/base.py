"""Declarative base, shared column types and mixins."""

from __future__ import annotations

from datetime import datetime
from typing import ClassVar

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: JSONB on PostgreSQL, plain JSON everywhere else (keeps tests portable).
JSONType = sa.JSON().with_variant(JSONB(astext_type=sa.Text()), "postgresql")


class Base(DeclarativeBase):
    """Project-wide declarative base."""

    type_annotation_map: ClassVar[dict] = {dict: JSONType, list: JSONType}


class TimestampMixin:
    """``created_at`` / ``updated_at`` maintained by the database."""

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )
