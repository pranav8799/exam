"""076_clean_old_pipeline_hierarchy_links

Revision ID: 076_clean_old_pipeline_hierarchy_links
Revises: 075_similar_questions_limit_setting
Create Date: 2026-11-21 00:00:00.000000

Papers processed by the old AI explanation pipeline (removed 2026-09-03) carry extra
`question_hierarchy_links`: one per AI-guessed concept, often in another subject, with unreliable
`is_primary` flags. Screens that read the primary link then showed the wrong subject/topic chips
(e.g. an Ecology & Environment question shown as Geography), and saving such a question in the
admin editor could silently move it to that subject.

For those papers only, each question is left with exactly one link: its own Nano Theme
(`questions.subtopic_id`), marked primary, the same shape the current extraction pipeline creates.
Questions, answers, explanations and students' responses are untouched.

Old-pipeline papers are recognised from the data: no question has a PDF subject tag
(`subject_raw`), and at least one link carries an AI concept name. Papers from the current
pipeline (PDF tags, links without concept names) are not touched.

Every link of an affected question is copied to `question_hierarchy_links_backup_076` first;
`downgrade` restores them from there.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "076_clean_old_pipeline_hierarchy_links"
down_revision: Union[str, None] = "075_similar_questions_limit_setting"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

BACKUP = "question_hierarchy_links_backup_076"

# Old-pipeline papers: no PDF subject tags anywhere, and AI concept names on their links
OLD_PIPELINE_EXAMS = """
    SELECT e.id
    FROM exams e
    WHERE EXISTS (SELECT 1 FROM questions q WHERE q.exam_id = e.id)
      AND NOT EXISTS (
          SELECT 1 FROM questions q
          WHERE q.exam_id = e.id AND COALESCE(TRIM(q.subject_raw), '') <> ''
      )
      AND EXISTS (
          SELECT 1 FROM question_hierarchy_links l JOIN questions q ON q.id = l.question_id
          WHERE q.exam_id = e.id AND COALESCE(TRIM(l.concept_name), '') <> ''
      )
"""

# Questions in those papers that have a Nano Theme and whose links are not already just that one link
AFFECTED_QUESTIONS = f"""
    SELECT q.id
    FROM questions q
    WHERE q.exam_id IN ({OLD_PIPELINE_EXAMS})
      AND q.subtopic_id IS NOT NULL
      AND (
          EXISTS (SELECT 1 FROM question_hierarchy_links l
                  WHERE l.question_id = q.id AND l.subtopic_id IS DISTINCT FROM q.subtopic_id)
          OR NOT EXISTS (SELECT 1 FROM question_hierarchy_links l
                         WHERE l.question_id = q.id AND l.subtopic_id = q.subtopic_id AND l.is_primary)
          OR (SELECT count(*) FROM question_hierarchy_links l WHERE l.question_id = q.id) <> 1
      )
"""


def upgrade() -> None:
    bind = op.get_bind()
    exams = bind.execute(sa.text(f"SELECT e.id, e.code FROM exams e WHERE e.id IN ({OLD_PIPELINE_EXAMS}) ORDER BY e.id")).fetchall()
    print(f"[076] Old-pipeline papers: {[(r[0], r[1]) for r in exams] or 'none'}")
    if not exams:
        return

    op.execute(f"DROP TABLE IF EXISTS {BACKUP}")
    op.execute(f"""
        CREATE TABLE {BACKUP} AS
        SELECT l.* FROM question_hierarchy_links l
        WHERE l.question_id IN ({AFFECTED_QUESTIONS})
    """)
    backed_up = bind.execute(sa.text(f"SELECT count(*), count(DISTINCT question_id) FROM {BACKUP}")).first()

    # 1. Drop every link that is not the question's own Nano Theme
    removed = bind.execute(sa.text(f"""
        DELETE FROM question_hierarchy_links l
        USING questions q
        WHERE l.question_id = q.id
          AND q.id IN (SELECT question_id FROM {BACKUP})
          AND l.subtopic_id IS DISTINCT FROM q.subtopic_id
    """)).rowcount

    # 2. Several links to the own Nano Theme: keep the oldest
    duplicates = bind.execute(sa.text(f"""
        DELETE FROM question_hierarchy_links l
        WHERE l.question_id IN (SELECT question_id FROM {BACKUP})
          AND l.id NOT IN (
              SELECT min(l2.id) FROM question_hierarchy_links l2
              WHERE l2.question_id IN (SELECT question_id FROM {BACKUP})
              GROUP BY l2.question_id
          )
    """)).rowcount

    # 3. The remaining link is the primary one
    bind.execute(sa.text(f"""
        UPDATE question_hierarchy_links SET is_primary = TRUE, relevance_weight = 1.0
        WHERE question_id IN (SELECT question_id FROM {BACKUP})
    """))

    # 4. Questions that had no link for their own Nano Theme get one (as the current pipeline creates)
    added = bind.execute(sa.text(f"""
        INSERT INTO question_hierarchy_links (question_id, subtopic_id, is_primary, relevance_weight)
        SELECT q.id, q.subtopic_id, TRUE, 1.0
        FROM questions q
        WHERE q.id IN (SELECT question_id FROM {BACKUP})
          AND NOT EXISTS (SELECT 1 FROM question_hierarchy_links l WHERE l.question_id = q.id)
    """)).rowcount

    print(
        f"[076] Backed up {backed_up[0]} links of {backed_up[1]} questions to {BACKUP}; "
        f"removed {removed} links to other Nano Themes and {duplicates} duplicates; added {added} own-Nano-Theme links."
    )


def downgrade() -> None:
    bind = op.get_bind()
    if BACKUP not in sa.inspect(bind).get_table_names():
        return
    bind.execute(sa.text(f"DELETE FROM question_hierarchy_links WHERE question_id IN (SELECT question_id FROM {BACKUP})"))
    bind.execute(sa.text(f"""
        INSERT INTO question_hierarchy_links (id, question_id, subtopic_id, topic_id, subject_id,
                                              is_primary, relevance_weight, concept_name)
        SELECT id, question_id, subtopic_id, topic_id, subject_id, is_primary, relevance_weight, concept_name
        FROM {BACKUP}
    """))
    op.execute(f"DROP TABLE {BACKUP}")
