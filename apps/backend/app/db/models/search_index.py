"""Precomputed similar questions and the runs that build them (see docs/SIMILAR_QUESTIONS.md)."""
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Field, SQLModel

from app.db.models.types import BigIntPK, DateTimeTZ, PortableJSON


def _now() -> datetime:
    return datetime.now(timezone.utc)


class QuestionSimilarityList(SQLModel, table=True):
    """Ranked similar questions for one published question in one mode.

    `items` is a list of {"id", "score", "raw", "reasons"} best first, up to
    SIMILARITY_STORED_LIST_SIZE entries. `complete` means the list holds every candidate the subject
    had at compute time (fewer than the stored size), so nothing is missing after filtering.
    """

    __tablename__ = "question_similarity_lists"

    question_id: int = Field(foreign_key="questions.id", primary_key=True, ondelete="CASCADE", sa_type=BigIntPK)
    mode: str = Field(primary_key=True, max_length=16)
    subject_key: str = Field(max_length=255, index=True)
    subject_signature: str = Field(max_length=64)
    algo_version: str = Field(max_length=64)
    items: Any = Field(default_factory=list, sa_type=PortableJSON)
    complete: bool = Field(default=False)
    computed_at: datetime = Field(default_factory=_now, sa_type=DateTimeTZ)


class SimilaritySubjectState(SQLModel, table=True):
    """The content signature a subject's lists were last fully computed from."""

    __tablename__ = "similarity_subject_state"

    subject_key: str = Field(primary_key=True, max_length=255)
    signature: str = Field(max_length=64)
    algo_version: str = Field(max_length=64)
    question_count: int = Field(default=0)
    computed_at: datetime = Field(default_factory=_now, sa_type=DateTimeTZ)


class SearchIndexRun(SQLModel, table=True):
    """One run of the search index (embeddings → BM25 statistics → similar lists)."""

    __tablename__ = "search_index_runs"

    id: Optional[int] = Field(default=None, primary_key=True, sa_type=BigIntPK)
    status: str = Field(max_length=20, index=True)  # queued | running | succeeded | warning | failed
    trigger: str = Field(max_length=20)  # cli | admin | upload
    triggered_by_id: Optional[int] = Field(default=None, foreign_key="users.id", ondelete="SET NULL", sa_type=BigIntPK)
    resumed_from_id: Optional[int] = Field(default=None, sa_type=BigIntPK)
    phase: Optional[str] = Field(default=None, max_length=32)  # embeddings | bm25 | similar_lists | done
    options: Optional[Any] = Field(default=None, sa_type=PortableJSON)
    estimate: Optional[Any] = Field(default=None, sa_type=PortableJSON)
    progress: Optional[Any] = Field(default=None, sa_type=PortableJSON)
    summary: Optional[Any] = Field(default=None, sa_type=PortableJSON)
    error_code: Optional[str] = Field(default=None, max_length=64)
    error_message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=_now, sa_type=DateTimeTZ)
    started_at: Optional[datetime] = Field(default=None, sa_type=DateTimeTZ)
    heartbeat_at: Optional[datetime] = Field(default=None, sa_type=DateTimeTZ)
    finished_at: Optional[datetime] = Field(default=None, sa_type=DateTimeTZ)
