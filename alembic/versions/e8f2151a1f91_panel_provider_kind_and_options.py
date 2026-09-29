"""panel provider kind and options

Revision ID: e8f2151a1f91
Revises: 0db50f46d11e
Create Date: 2026-09-29 16:32:22.893454+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8f2151a1f91"
down_revision: str | None = "0db50f46d11e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``server_default`` matters: an existing installation already has panel
    # rows, and a NOT NULL column without a default would fail to add.
    op.add_column(
        "panels",
        sa.Column("kind", sa.String(length=32), nullable=False, server_default="wg_guard"),
    )
    op.add_column(
        "panels",
        sa.Column(
            "options",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=True,
        ),
    )
    op.create_index(op.f("ix_panels_kind"), "panels", ["kind"], unique=False)
    # The server default has done its job; the model's Python-side default takes
    # over from here so the schema matches ``Base.metadata``.
    op.alter_column("panels", "kind", server_default=None)


def downgrade() -> None:
    op.drop_index(op.f("ix_panels_kind"), table_name="panels")
    op.drop_column("panels", "options")
    op.drop_column("panels", "kind")
    # ### end Alembic commands ###
