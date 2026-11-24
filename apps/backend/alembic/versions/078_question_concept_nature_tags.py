"""078_question_concept_nature_tags

Revision ID: 078_question_concept_nature_tags
Revises: 077_precomputed_similar_questions
Create Date: 2026-11-24 00:00:00.000000

Question papers now carry three more header tags per question:
[Core Concept: ...] [Nature: Static – Basic] [Approach: Factual]. They are stored as raw text next to
subject_raw / topic_raw / subtopic_raw / difficulty_raw. All three are nullable: questions from older
papers (and older papers uploaded later) simply have none.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "078_question_concept_nature_tags"
down_revision: Union[str, None] = "077_precomputed_similar_questions"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

NEW_COLUMNS = (
    ("core_concept_raw", 255),
    ("nature_raw", 100),
    ("approach_raw", 100),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "questions" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("questions")}
    for name, length in NEW_COLUMNS:
        if name not in existing:
            op.add_column("questions", sa.Column(name, sa.String(length=length), nullable=True))


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {c["name"] for c in inspector.get_columns("questions")}
    for name, _ in reversed(NEW_COLUMNS):
        if name in existing:
            op.drop_column("questions", name)
