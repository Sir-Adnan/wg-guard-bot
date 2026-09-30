"""menu layout

The owner can rearrange, hide and add main-menu buttons, so the layout is stored
instead of hard-coded.  Rows are sparse on purpose: a row exists only for a
button the operator moved, hid or added, and everything else falls back to the
default in ``app/services/menu_layout.py`` — which is what lets a later release
introduce a menu entry without a data migration.

Revision ID: a1d0c7f4b2e6
Revises: c4b7d1e90a35
Create Date: 2026-05-18 11:02:44.117903+00:00

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1d0c7f4b2e6"
down_revision: str | None = "c4b7d1e90a35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "menu_layout",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("is_visible", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    op.drop_table("menu_layout")
