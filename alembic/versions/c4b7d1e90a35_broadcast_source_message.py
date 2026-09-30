"""broadcast source message

A broadcast composed inside the bot keeps its origin message and is delivered
with ``copyMessage``, so premium emoji, formatting, media and the inline
keyboard survive untouched.  Both columns are nullable: every campaign created
from the panel keeps the plain text/media path.

Revision ID: c4b7d1e90a35
Revises: e8f2151a1f91
Create Date: 2026-05-18 09:14:37.412008+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4b7d1e90a35"
down_revision: str | None = "e8f2151a1f91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("broadcasts", sa.Column("source_chat_id", sa.BigInteger(), nullable=True))
    op.add_column("broadcasts", sa.Column("source_message_id", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("broadcasts", "source_message_id")
    op.drop_column("broadcasts", "source_chat_id")
