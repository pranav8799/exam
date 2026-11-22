"""077_precomputed_similar_questions

Revision ID: 077_precomputed_similar_questions
Revises: 076_clean_old_pipeline_hierarchy_links
Create Date: 2026-11-22 00:00:00.000000

Similar questions are computed ahead of time by the search-index run (the backfill) instead of on
every page view.

- question_similarity_lists : one ranked list per published question and mode ("default",
  "other_years"), with the subject it was computed for and the algorithm version.
- similarity_subject_state  : per subject, the content signature its lists were computed from;
  a different signature means the subject's lists are out of date.
- search_index_runs         : every run of the search index (CLI, admin dashboard, PDF upload):
  status, phase, progress, estimate, failure reason, summary. Used for the admin page and to resume.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "077_precomputed_similar_questions"
down_revision: Union[str, None] = "076_clean_old_pipeline_hierarchy_links"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.DateTime(timezone=True)
NOW = sa.text("CURRENT_TIMESTAMP")
JSONB = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())

    if "question_similarity_lists" not in tables:
        op.create_table(
            "question_similarity_lists",
            sa.Column("question_id", sa.BigInteger(), sa.ForeignKey("questions.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("mode", sa.String(16), primary_key=True),
            sa.Column("subject_key", sa.String(255), nullable=False),
            sa.Column("subject_signature", sa.String(64), nullable=False),
            sa.Column("algo_version", sa.String(64), nullable=False),
            sa.Column("items", JSONB, nullable=False),
            sa.Column("complete", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("computed_at", TS, nullable=False, server_default=NOW),
        )
        op.create_index("ix_qsl_subject_key", "question_similarity_lists", ["subject_key"])

    if "similarity_subject_state" not in tables:
        op.create_table(
            "similarity_subject_state",
            sa.Column("subject_key", sa.String(255), primary_key=True),
            sa.Column("signature", sa.String(64), nullable=False),
            sa.Column("algo_version", sa.String(64), nullable=False),
            sa.Column("question_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("computed_at", TS, nullable=False, server_default=NOW),
        )

    if "search_index_runs" not in tables:
        op.create_table(
            "search_index_runs",
            sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("trigger", sa.String(20), nullable=False),
            sa.Column("triggered_by_id", sa.BigInteger(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("resumed_from_id", sa.BigInteger(), nullable=True),
            sa.Column("phase", sa.String(32), nullable=True),
            sa.Column("options", JSONB, nullable=True),
            sa.Column("estimate", JSONB, nullable=True),
            sa.Column("progress", JSONB, nullable=True),
            sa.Column("summary", JSONB, nullable=True),
            sa.Column("error_code", sa.String(64), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", TS, nullable=False, server_default=NOW),
            sa.Column("started_at", TS, nullable=True),
            sa.Column("heartbeat_at", TS, nullable=True),
            sa.Column("finished_at", TS, nullable=True),
        )
        op.create_index("ix_search_index_runs_status", "search_index_runs", ["status"])


def downgrade() -> None:
    op.drop_table("search_index_runs")
    op.drop_table("similarity_subject_state")
    op.drop_table("question_similarity_lists")
