"""Read precomputed similar-question lists (written by search-index runs, see search_index.py).

A stored list is used only when it is still trustworthy; otherwise the caller searches live:
- it was computed with the current algorithm version (settings + code), and
- the question is still in the subject the list was computed for, and
- after re-checking every item (still published, still the same subject) and removing the
  excluded ids, enough items are left, or the list is `complete` (it holds the whole subject).

Lists can be older than the latest edits until the next run (a new paper's questions appear in
other questions' lists after its run). The admin page shows how many subjects are out of date.
"""
from typing import Any, Dict, List, Optional, Set

from sqlalchemy import func
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.subjects import normalize_subject
from app.db.models.enums import QuestionStatus
from app.db.models.hierarchy import Subject, Subtopic, Topic
from app.db.models.question import Question
from app.db.models.search_index import QuestionSimilarityList


async def question_subject_keys(session: AsyncSession, ids: List[int]) -> Dict[int, tuple]:
    """{id: (normalized subject, status)} using the search's subject rule (hierarchy first, then the PDF tag)."""
    if not ids:
        return {}
    rows = (await session.exec(
        select(Question.id, Question.status, Question.subject_raw, Subject.name)
        .select_from(Question)
        .join(Subtopic, Subtopic.id == Question.subtopic_id, isouter=True)
        .join(Topic, Topic.id == Subtopic.topic_id, isouter=True)
        .join(Subject, Subject.id == Topic.subject_id, isouter=True)
        .where(col(Question.id).in_(ids))
    )).all()
    out = {}
    for qid, status, raw, hierarchy_name in rows:
        name = hierarchy_name or ((raw or "").strip() or None)
        out[qid] = (normalize_subject(name), status)
    return out


async def read_stored(
    session: AsyncSession,
    question_id: int,
    mode: str,
    limit: int,
    excluded: Optional[Set[int]] = None,
    version: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Up to `limit` matches {question_id, similarity_score, match_reasons, raw_score, ranking}, or None to
    search live. Without `version`, a list built with either ranking (RRF or Jina reranker) is accepted, as
    long as it matches the current settings: whichever kind of similar-lists run ran last is what students see."""
    from app.services.vector.search_index import current_versions

    versions = current_versions() if version is None else {version: None}
    row = await session.get(QuestionSimilarityList, (question_id, mode))
    if row is None or row.algo_version not in versions:
        return None
    list_ranking = versions[row.algo_version]
    items = list(row.items or [])
    ids = [question_id] + [int(i["id"]) for i in items]
    info = await question_subject_keys(session, ids)
    own = info.get(question_id)
    if own is None or not own[0] or own[0] != row.subject_key:
        return None
    excluded = excluded or set()
    kept = []
    for item in items:
        qid = int(item["id"])
        if qid in excluded or qid == question_id:
            continue
        subject_key, status = info.get(qid, ("", None))
        if status != QuestionStatus.PUBLISHED or subject_key != row.subject_key:
            continue  # unpublished or moved since the list was computed
        kept.append({
            "question_id": qid,
            "similarity_score": int(item.get("score", 0)),
            "match_reasons": list(item.get("reasons") or []),
            "raw_score": float(item.get("raw", 0.0)),
            **({"ranking": item.get("ranking") or list_ranking} if (item.get("ranking") or list_ranking) else {}),
        })
        if len(kept) >= limit:
            break
    # Too few left: a live search may fill the slots, unless the list already held the whole subject
    # (then a live search has no other candidates either)
    if len(kept) < limit and not row.complete:
        return None
    return kept


async def stored_list_count(session: AsyncSession) -> int:
    return (await session.exec(select(func.count()).select_from(QuestionSimilarityList))).one()
