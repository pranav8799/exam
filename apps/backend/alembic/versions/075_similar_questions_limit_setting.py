"""075_similar_questions_limit_setting

Revision ID: 075_similar_questions_limit_setting
Revises: 074_explanation_embedding_and_bm25
Create Date: 2026-11-21 00:00:00.000000

The number of similar questions shown to students is now an admin setting (site_settings) instead
of being fixed in the frontend. Also seeds the `admin.similar_questions` section permission.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "075_similar_questions_limit_setting"
down_revision: Union[str, None] = "074_explanation_embedding_and_bm25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_LIMIT = 6
PERMISSION = ("admin.similar_questions", "Similar Questions", "Set how many similar questions students see.")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "site_settings" in inspector.get_table_names():
        columns = {c["name"] for c in inspector.get_columns("site_settings")}
        if "similar_questions_limit" not in columns:
            op.add_column(
                "site_settings",
                sa.Column("similar_questions_limit", sa.Integer(), nullable=False,
                          server_default=sa.text(str(DEFAULT_LIMIT))),
            )

    if "permissions" in inspector.get_table_names():
        permissions = sa.table(
            "permissions",
            sa.column("key", sa.String),
            sa.column("label", sa.String),
            sa.column("description", sa.String),
            sa.column("created_at", sa.DateTime(timezone=True)),
        )
        existing = {row[0] for row in bind.execute(sa.select(permissions.c.key))}
        key, label, description = PERMISSION
        if key not in existing:
            bind.execute(
                permissions.insert().values(key=key, label=label, description=description, created_at=sa.func.now())
            )


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM permissions WHERE key = :key").bindparams(key=PERMISSION[0]))
    op.drop_column("site_settings", "similar_questions_limit")
