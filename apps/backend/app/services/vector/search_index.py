"""Search index: embeddings → BM25 statistics → precomputed similar questions, as tracked runs.

A run is started from the CLI (`backfill_question_embeddings` for vectors, `build_similar_lists` for
lists) or the admin dashboard (Admin → Similar Questions); a PDF upload no longer starts one (since
2026-11-24; older runs may still show trigger "upload"). Each run is a row in `search_index_runs` with
its phase, progress, estimate, summary and, if it stopped, a plain reason.

Run kinds (`options["kind"]`)
  "vectors"        phases 1–2: embeddings (Gemini) and BM25 statistics.
  "similar_lists"  phases 2–3: BM25 statistics (no API cost) and similar lists. `options["use_reranker"]`
                   chooses the ranking: False = fused ranking (RRF) only, True = Jina reranker.
  "full"           all three phases (the original behaviour; `skip_similar` / `skip_embeddings` still work).
Vectors are built once; lists can then be rebuilt as often as needed, with or without Jina, at no Gemini cost.

Phases
  1. embeddings     embed new/changed question texts and solutions (hash check; batch API)
  2. bm25           recompute BM25 corpus statistics
  3. similar_lists  for every subject whose content changed, compute each published question's
                    ranked similar list ("default" and "other_years") and store it

Ranking and versions: the algorithm version includes the ranking ("rrf" or "rerank"), so lists built one
way are out of date for a run of the other way (switching rebuilds every subject). Students are served
whichever stored list exists, as long as its version is current for one of the two rankings.

Why per subject: a question's similar list only contains questions of the same subject, so adding,
editing, publishing or unpublishing questions can only change lists inside their subject. A subject's
*signature* (a hash over its published questions, their papers/years and their text hashes) tells
whether its lists are out of date; the algorithm version (settings + code) forces a full rebuild when
the search itself changes.

Resume: every step is idempotent. Embeddings already saved match their hash; lists already stored
match their subject signature and version. Starting a run after a failure therefore continues where
the previous one stopped, without paying for finished work again.
"""
import asyncio
import hashlib
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from sqlalchemy import delete, func, or_
from sqlmodel import col, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.config.logger import setup_logger
from app.config.settings import get_settings
from app.core.subjects import normalize_subject
from app.db.models.enums import QuestionStatus
from app.db.models.hierarchy import Exam, Subject, Subtopic, Topic
from app.db.models.question import Question
from app.db.models.question_embedding import QuestionEmbedding
from app.db.models.search_index import QuestionSimilarityList, SearchIndexRun, SimilaritySubjectState
from app.services.vector.provider_errors import ProviderError, classify

logger = setup_logger("SearchIndex")

MODES = ("default", "other_years")
ALGO_CODE_VERSION = "1"  # bump when the search code changes in a way that should rebuild every list

# Rough cost model for estimates (an admin warning, not a bill)
CHARS_PER_TOKEN = 4
GEMINI_SECONDS_PER_REQUEST = 2.0
JINA_SECONDS_PER_CALL = 0.9
RRF_SECONDS_PER_LIST = 0.08  # one list without the reranker (~50 ms search + write)
LIST_CONCURRENCY = 4        # similar lists computed at the same time (Jina calls in flight)

KIND_VECTORS, KIND_LISTS, KIND_FULL = "vectors", "similar_lists", "full"
RUN_KINDS = (KIND_VECTORS, KIND_LISTS, KIND_FULL)
PROGRESS_EVERY_LISTS = 40   # progress / heartbeat saved after this many lists

FATAL_PROVIDER_CODES_SUFFIX = ("_auth", "_no_credits", "_quota", "_concurrency", "_no_key", "_unreachable", "_server")

STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_WARNING, STATUS_FAILED = (
    "queued", "running", "succeeded", "warning", "failed",
)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)


class RunAlreadyActive(Exception):
    def __init__(self, run_id: int):
        super().__init__(f"Search index run #{run_id} is already in progress.")
        self.run_id = run_id


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ranking_name(use_reranker: bool) -> str:
    return "rerank" if use_reranker else "rrf"


def algo_version(use_reranker: bool = False) -> str:
    """Everything that changes what a stored list should contain; a different value rebuilds all lists.

    The ranking is part of it: lists ranked by the fused score (RRF) and lists reranked by Jina have
    different versions. Reranker settings only count when the reranker is used."""
    s = get_settings()
    material = {
        "code": ALGO_CODE_VERSION,
        "ranking": ranking_name(use_reranker),
        "weights": [s.SIMILARITY_WEIGHT_QUESTION_DENSE, s.SIMILARITY_WEIGHT_QUESTION_BM25,
                    s.SIMILARITY_WEIGHT_EXPLANATION_DENSE, s.SIMILARITY_WEIGHT_ANSWER_BM25,
                    s.SIMILARITY_WEIGHT_ANSWER_IN_QUESTION_BM25],
        "bm25": [s.SIMILARITY_BM25_K1, s.SIMILARITY_BM25_B],
        "rrf_k": s.SIMILARITY_RRF_K,
        "channel_limit": s.SIMILARITY_CHANNEL_LIMIT,
        "stored": s.SIMILARITY_STORED_LIST_SIZE,
        "embedding": [s.GEMINI_EMBEDDING_MODEL, s.GEMINI_EMBEDDING_DIM],
    }
    if use_reranker:
        material["rerank_top_n"] = s.SIMILARITY_RERANK_TOP_N
        material["reranker"] = s.JINA_RERANKER_MODEL
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:16]


def current_versions() -> Dict[str, str]:
    """{algo version: ranking} for both rankings; a stored list with either version may be served."""
    return {algo_version(False): "rrf", algo_version(True): "rerank"}


# ── Subjects and their signatures ────────────────────────────────────────────

@dataclass
class SubjectInfo:
    key: str
    name: str
    published_ids: List[int] = field(default_factory=list)
    signature: str = ""


async def subject_snapshot(session: AsyncSession, use_reranker: bool = False) -> Dict[str, SubjectInfo]:
    """Published questions grouped by subject (same rule as the search) with each subject's signature
    (for the given ranking, since the algorithm version is part of the signature)."""
    rows = (await session.exec(
        select(
            Question.id, Question.status, Question.exam_id, Question.source_year, Exam.source_year,
            Question.subject_raw, Subject.name,
            QuestionEmbedding.question_hash, QuestionEmbedding.answer_hash, QuestionEmbedding.explanation_hash,
        )
        .select_from(Question)
        .join(Exam, Exam.id == Question.exam_id, isouter=True)
        .join(Subtopic, Subtopic.id == Question.subtopic_id, isouter=True)
        .join(Topic, Topic.id == Subtopic.topic_id, isouter=True)
        .join(Subject, Subject.id == Topic.subject_id, isouter=True)
        .join(QuestionEmbedding, QuestionEmbedding.question_id == Question.id, isouter=True)
        .where(Question.status == QuestionStatus.PUBLISHED)
    )).all()
    subjects: Dict[str, SubjectInfo] = {}
    material: Dict[str, List[str]] = defaultdict(list)
    spellings: Dict[str, Counter] = defaultdict(Counter)
    for qid, _status, exam_id, q_year, e_year, raw, hierarchy_name, q_hash, a_hash, e_hash in rows:
        name = hierarchy_name or ((raw or "").strip() or None)
        key = normalize_subject(name)
        if not key:
            continue
        info = subjects.setdefault(key, SubjectInfo(key=key, name=name))
        info.published_ids.append(qid)
        # Display name: the syllabus name if any question has one, else the most common spelling
        spellings[key][(1 if hierarchy_name else 0, name)] += 1
        material[key].append(f"{qid}|{exam_id}|{q_year or e_year}|{q_hash}|{a_hash}|{e_hash}")
    version = algo_version(use_reranker)
    for key, info in subjects.items():
        info.name = max(spellings[key].items(), key=lambda kv: (kv[0][0], kv[1], kv[0][1]))[0][1]
        info.published_ids.sort()
        digest = hashlib.sha256((version + "\n" + "\n".join(sorted(material[key]))).encode()).hexdigest()
        info.signature = digest
    return subjects


async def _subject_states(session: AsyncSession) -> Dict[str, SimilaritySubjectState]:
    return {s.subject_key: s for s in (await session.exec(select(SimilaritySubjectState))).all()}


def _is_dirty(info: SubjectInfo, state: Optional[SimilaritySubjectState], version: Optional[str] = None) -> bool:
    return state is None or state.signature != info.signature or state.algo_version != (version or algo_version())


# ── Plan (what a run would do) and estimate ──────────────────────────────────

async def build_plan(session_factory, exam_id: Optional[int] = None, force: bool = False,
                     use_reranker: bool = False, kind: str = KIND_FULL) -> Dict[str, Any]:
    """What a run would do now, with an estimate of the paid API usage. Read-only.

    `kind`: "vectors" counts only embeddings, "similar_lists" only lists (for the given ranking),
    "full" both (lists also for subjects whose embeddings the run would change)."""
    from app.scripts.backfill_question_embeddings import fetch_question_candidates

    s = get_settings()
    with_vectors = kind in (KIND_VECTORS, KIND_FULL)
    with_lists = kind in (KIND_LISTS, KIND_FULL)
    candidates = await fetch_question_candidates(exam_id=exam_id, force=force, session_factory=session_factory)
    pending = [c for c in candidates if not c["is_uptodate"]]
    q_texts = [c["question_text"] for c in pending if c["embed_question"] and c["question_text"]]
    e_texts = [c["explanation_text"] for c in pending if c["embed_explanation"] and c["explanation_text"]]
    pending_published_ids = {c["question_id"] for c in pending if c.get("published")}

    async with session_factory() as session:
        subjects = await subject_snapshot(session, use_reranker)
        states = await _subject_states(session)
        stored_rows = (await session.exec(
            select(QuestionSimilarityList.question_id, QuestionSimilarityList.mode,
                   QuestionSimilarityList.subject_signature, QuestionSimilarityList.algo_version)
        )).all()
        total_questions = (await session.exec(select(func.count()).select_from(Question))).one()
        published_total = sum(len(i.published_ids) for i in subjects.values())
        no_subject_published = (await session.exec(
            select(func.count()).select_from(Question).where(Question.status == QuestionStatus.PUBLISHED)
        )).one() - published_total
        text_lengths = await _avg_doc_chars(session)

    version = algo_version(use_reranker)
    versions = current_versions()
    done = {(q, m) for q, m, sig, v in stored_rows if v == version}
    stored_by_ranking = {"rrf": 0, "rerank": 0, "outdated": 0}
    for _q, _m, _sig, v in stored_rows:
        stored_by_ranking[versions.get(v, "outdated")] += 1
    stored_sig = {(q, m): sig for q, m, sig, v in stored_rows}
    dirty: List[SubjectInfo] = []
    lists_to_compute = 0
    jina_tokens = 0
    for info in sorted(subjects.values(), key=lambda i: i.key):
        # In a full run, lists of subjects whose embeddings change are recomputed too
        changes_with_embeddings = kind == KIND_FULL and any(q in pending_published_ids for q in info.published_ids)
        if not (_is_dirty(info, states.get(info.key), version) or changes_with_embeddings):
            continue
        dirty.append(info)
        pool = len(info.published_ids) - 1
        docs = min(pool, s.SIMILARITY_RERANK_TOP_N)
        for qid in info.published_ids:
            for mode in MODES:
                # A list already stored for this exact content and version is skipped (resume)
                if not changes_with_embeddings and (qid, mode) in done and stored_sig.get((qid, mode)) == info.signature:
                    continue
                lists_to_compute += 1
                if use_reranker:
                    jina_tokens += (docs * text_lengths["doc"] + text_lengths["doc"]) / CHARS_PER_TOKEN

    if not with_vectors:
        q_count, e_count, gemini_chars, gemini_requests = 0, 0, 0, 0
    else:
        q_count, e_count = len(q_texts), len(e_texts)
        gemini_chars = sum(len(t) for t in q_texts) + sum(len(t) for t in e_texts)
        gemini_requests = math.ceil(q_count / 100) + math.ceil(e_count / 100)
    if not with_lists:
        dirty, lists_to_compute, jina_tokens = [], 0, 0
    jina_calls = lists_to_compute if (dirty and use_reranker) else 0
    vectors_minutes = gemini_requests * GEMINI_SECONDS_PER_REQUEST / 60
    if use_reranker:
        # The slower of: parallel calls, or the key's tokens-per-minute pace
        lists_minutes = max(
            jina_calls * JINA_SECONDS_PER_CALL / max(1, min(LIST_CONCURRENCY, s.JINA_RUN_MAX_CONCURRENT_REQUESTS)) / 60,
            jina_tokens / max(1, s.JINA_RERANK_TOKENS_PER_MINUTE),
        )
    else:
        lists_minutes = lists_to_compute * RRF_SECONDS_PER_LIST / LIST_CONCURRENCY / 60
    return {
        "algo_version": version,
        "kind": kind,
        "ranking": ranking_name(use_reranker),
        "totals": {
            "questions": total_questions,
            "published_with_subject": published_total,
            "published_without_subject": max(0, no_subject_published),
            "subjects": len(subjects),
            "stored_lists": len(done),
            "stored_lists_by_ranking": stored_by_ranking,
        },
        "pending": {
            "question_embeddings": q_count,
            "solution_embeddings": e_count,
            # Always reported, so a lists-only plan can warn that some vectors are not built yet
            "embeddings_not_built": len(q_texts) + len(e_texts),
            "subjects_out_of_date": len(dirty),
            "lists_to_compute": lists_to_compute,
            "out_of_date_subjects": [{"subject": i.name, "published_questions": len(i.published_ids)} for i in dirty[:50]],
        },
        "estimate": {
            "gemini": {"requests": gemini_requests, "texts": q_count + e_count,
                       "approx_tokens": int(gemini_chars / CHARS_PER_TOKEN)},
            "jina": {"rerank_calls": jina_calls, "approx_tokens": int(jina_tokens)},
            "approx_minutes": round(vectors_minutes + lists_minutes, 1),
            "minutes": {"vectors": round(vectors_minutes, 1), "similar_lists": round(lists_minutes, 1)},
            "note": "Approximate. Gemini and Jina do not expose account balances to this app: "
                    "check you have enough credits before starting."
                    + ("" if use_reranker or not with_lists else " Lists without the reranker make no paid API calls."),
        },
        "up_to_date": not q_count and not e_count and not dirty,
    }


async def _avg_doc_chars(session: AsyncSession) -> Dict[str, float]:
    """Average characters of a reranker document ("Question: … Correct Answer: …")."""
    from app.db.models.question import QuestionTranslation
    avg_q = (await session.exec(select(func.avg(func.length(QuestionTranslation.question_text))))).one() or 0
    return {"doc": float(avg_q) + 40.0}


# ── Runs ─────────────────────────────────────────────────────────────────────

ProgressCallback = Callable[[SearchIndexRun], Awaitable[None]]


class SearchIndexRunner:
    """Creates and executes search-index runs. One active run at a time."""

    def __init__(self, session_factory=None, embedder=None, service_factory=None, list_concurrency: int = LIST_CONCURRENCY):
        if session_factory is None:
            from app.db.session import async_session_factory
            session_factory = async_session_factory
        self.session_factory = session_factory
        self._embedder = embedder
        self._service_factory = service_factory
        self.list_concurrency = max(1, list_concurrency)
        from app.services.vector.rate_limiter import TokenRateLimiter
        self.limiter = TokenRateLimiter(get_settings().JINA_RERANK_TOKENS_PER_MINUTE,
                                        max_concurrent=get_settings().JINA_RUN_MAX_CONCURRENT_REQUESTS)

    # -- lifecycle ------------------------------------------------------------

    async def active_run(self, session: AsyncSession) -> Optional[SearchIndexRun]:
        """The run in progress, if any. A 'running' run whose heartbeat stopped is closed as interrupted."""
        stale_after = timedelta(minutes=get_settings().SEARCH_INDEX_STALE_RUN_MINUTES)
        runs = (await session.exec(
            select(SearchIndexRun).where(col(SearchIndexRun.status).in_(ACTIVE_STATUSES)).order_by(col(SearchIndexRun.id))
        )).all()
        active = None
        for run in runs:
            last_seen = _aware(run.heartbeat_at or run.started_at or run.created_at)
            if last_seen and _now() - last_seen > stale_after:
                run.status = STATUS_FAILED
                run.error_code = "interrupted"
                run.error_message = ("The run stopped without finishing (the server or script was stopped). "
                                     "Start a new run; it resumes where this one stopped.")
                run.finished_at = _now()
                session.add(run)
            else:
                active = run
        await session.commit()
        return active

    async def create_run(self, trigger: str, user_id: Optional[int] = None,
                         options: Optional[Dict[str, Any]] = None) -> SearchIndexRun:
        options = dict(options or {})
        options["kind"] = options.get("kind") or KIND_FULL
        if options["kind"] not in RUN_KINDS:
            raise ValueError(f"Unknown run kind {options['kind']!r}; expected one of {RUN_KINDS}")
        options["use_reranker"] = bool(options.get("use_reranker"))
        async with self.session_factory() as session:
            active = await self.active_run(session)
            if active is not None:
                raise RunAlreadyActive(active.id)
            last = (await session.exec(select(SearchIndexRun).order_by(col(SearchIndexRun.id).desc()).limit(1))).first()
        plan = await build_plan(self.session_factory, exam_id=options.get("exam_id"), force=bool(options.get("force")),
                                use_reranker=options["use_reranker"], kind=options["kind"])
        async with self.session_factory() as session:
            # Re-check: another run may have been created while the plan was computed
            active = await self.active_run(session)
            if active is not None:
                raise RunAlreadyActive(active.id)
            run = SearchIndexRun(
                status=STATUS_QUEUED,
                trigger=trigger,
                triggered_by_id=user_id,
                resumed_from_id=last.id if last is not None and last.status == STATUS_FAILED else None,
                options=options,
                estimate={"pending": plan["pending"], "estimate": plan["estimate"], "totals": plan["totals"]},
                progress={},
            )
            session.add(run)
            await session.commit()
            await session.refresh(run)
            # Two starts at the same instant both pass the check above: the older run wins
            first = (await session.exec(
                select(SearchIndexRun).where(col(SearchIndexRun.status).in_(ACTIVE_STATUSES))
                .order_by(col(SearchIndexRun.id)).limit(1)
            )).first()
            if first is not None and first.id != run.id:
                await session.delete(run)
                await session.commit()
                raise RunAlreadyActive(first.id)
            return run

    async def execute(self, run_id: int, on_progress: Optional[ProgressCallback] = None) -> SearchIndexRun:
        """Run all phases for an existing run. Never raises: the outcome is stored on the run."""
        async with self.session_factory() as session:
            run = await session.get(SearchIndexRun, run_id)
            run.status, run.started_at, run.heartbeat_at = STATUS_RUNNING, _now(), _now()
            run.progress = {}
            session.add(run)
            await session.commit()
        progress: Dict[str, Any] = {}
        options = dict(run.options or {})
        kind = options.get("kind") or KIND_FULL
        do_embeddings = kind in (KIND_VECTORS, KIND_FULL) and not options.get("skip_embeddings")
        do_lists = kind in (KIND_LISTS, KIND_FULL) and not options.get("skip_similar")
        try:
            if do_embeddings:
                await self._save(run_id, phase="embeddings", progress=progress, on_progress=on_progress)
                progress["embeddings"] = await self._phase_embeddings(run_id, options, progress, on_progress)
            # BM25 statistics come from the stored tsvectors (no API cost); every kind refreshes them
            await self._save(run_id, phase="bm25", progress=progress, on_progress=on_progress)
            progress["bm25"] = await self._phase_bm25()
            if do_lists:
                await self._save(run_id, phase="similar_lists", progress=progress, on_progress=on_progress)
                try:
                    progress["similar_lists"] = await self._phase_similar_lists(run_id, progress, on_progress, options)
                except ExceptionGroup as group:  # from asyncio.TaskGroup: report the first real error
                    raise group.exceptions[0]
            emb = progress.get("embeddings", {})
            failed_texts = emb.get("question_failed", 0) + emb.get("solution_failed", 0)
            failed_lists = progress.get("similar_lists", {}).get("lists_failed", 0)
            status = STATUS_WARNING if failed_texts or failed_lists else STATUS_SUCCEEDED
            summary = _summary(progress)
            warnings = []
            if failed_texts:
                warnings.append(f"{failed_texts} text(s) could not be embedded; they are retried on the next run.")
            if failed_lists:
                warnings.append(f"{failed_lists} similar list(s) could not be computed (see progress → failed_examples); "
                                "those questions use live search and are retried on the next run.")
            if warnings:
                summary["warning"] = " ".join(warnings)
            return await self._finish(run_id, status, progress, summary=summary, on_progress=on_progress)
        except ProviderError as err:
            logger.error(f"[SEARCH-INDEX] Run #{run_id} stopped: {err.code} — {err.message}")
            return await self._finish(run_id, STATUS_FAILED, progress, error=(err.code, err.message), on_progress=on_progress)
        except Exception as err:  # the outcome must always reach the run row
            logger.exception(f"[SEARCH-INDEX] Run #{run_id} failed unexpectedly")
            return await self._finish(run_id, STATUS_FAILED, progress,
                                      error=("unexpected", f"Unexpected error: {type(err).__name__}: {str(err)[:400]}"),
                                      on_progress=on_progress)

    async def run(self, trigger: str, user_id: Optional[int] = None, options: Optional[Dict[str, Any]] = None,
                  on_progress: Optional[ProgressCallback] = None) -> SearchIndexRun:
        run = await self.create_run(trigger, user_id=user_id, options=options)
        return await self.execute(run.id, on_progress=on_progress)

    # -- phases ---------------------------------------------------------------

    def _get_embedder(self):
        if self._embedder is None:
            from app.services.vector.gemini_embedder import GeminiEmbedder
            self._embedder = GeminiEmbedder()
        return self._embedder

    async def _phase_embeddings(self, run_id, options, progress, on_progress) -> Dict[str, Any]:
        from app.scripts.backfill_question_embeddings import fetch_question_candidates, process_batch
        from app.services.vector.provider_errors import missing_key

        candidates = await fetch_question_candidates(
            exam_id=options.get("exam_id"), limit=options.get("limit"), force=bool(options.get("force")),
            session_factory=self.session_factory,
        )
        to_process = [c for c in candidates if not c["is_uptodate"]]
        stats: Dict[str, Any] = {"to_process": len(to_process), "processed": 0}
        progress["embeddings"] = stats
        if not to_process:
            return stats
        embedder = self._get_embedder()
        if not getattr(embedder, "api_key", None):
            raise missing_key("gemini")
        batch_size = int(options.get("batch_size") or 100)
        for start in range(0, len(to_process), batch_size):
            batch = to_process[start:start + batch_size]
            embedder.last_failure = None
            batch_stats: Dict[str, int] = {}
            await process_batch(batch, embedder, stats=batch_stats)
            for k, v in batch_stats.items():
                stats[k] = stats.get(k, 0) + v
            stats["processed"] += len(batch)
            failed = batch_stats.get("question_failed", 0) + batch_stats.get("solution_failed", 0)
            failure = getattr(embedder, "last_failure", None)
            if failed and failure is not None:
                err = classify(failure)
                if err.code.endswith(FATAL_PROVIDER_CODES_SUFFIX):
                    raise err
            await self._save(run_id, progress=progress, on_progress=on_progress)
        return stats

    async def _phase_bm25(self) -> Dict[str, Any]:
        from app.services.vector.bm25 import refresh_bm25_stats
        async with self.session_factory() as session:
            if session.get_bind().dialect.name != "postgresql":
                return {"skipped": "not PostgreSQL"}
            return {"documents": await refresh_bm25_stats(session)}

    def _make_service(self, session: AsyncSession, use_reranker: bool = False):
        if self._service_factory is not None:
            return self._service_factory(session)
        from app.services.vector.similarity_retrieval_service import HybridSimilarityService
        service = HybridSimilarityService(session, strict_reranker=use_reranker, use_reranker=use_reranker)
        if use_reranker:
            service.reranker.limiter = self.limiter  # one pace for all parallel lists of this run
        return service

    async def _phase_similar_lists(self, run_id, progress, on_progress, options=None) -> Dict[str, Any]:
        settings = get_settings()
        stored_size = settings.SIMILARITY_STORED_LIST_SIZE
        use_reranker = bool((options or {}).get("use_reranker"))
        if use_reranker and self._service_factory is None and not settings.JINA_API_KEY:
            from app.services.vector.provider_errors import missing_key
            raise missing_key("jina")  # stop before deleting or recomputing anything
        version = algo_version(use_reranker)
        async with self.session_factory() as session:
            subjects = await subject_snapshot(session, use_reranker)
            states = await _subject_states(session)
            removed = await self._remove_stale_lists(session, subjects)

        dirty = [i for i in sorted(subjects.values(), key=lambda i: i.key) if _is_dirty(i, states.get(i.key), version)]
        only = {normalize_subject(x) for x in (options or {}).get("subjects") or []}
        if only:  # e.g. a trial run on one small subject
            dirty = [i for i in dirty if i.key in only]
        stats: Dict[str, Any] = {
            "ranking": ranking_name(use_reranker),
            "subjects_to_update": len(dirty), "subjects_done": 0,
            "lists_total": sum(len(i.published_ids) for i in dirty) * len(MODES),
            "lists_computed": 0, "lists_reused": 0, "lists_removed": removed,
            "lists_failed": 0, "failed_examples": [],
        }
        progress["similar_lists"] = stats
        await self._save(run_id, progress=progress, on_progress=on_progress)

        for info in dirty:
            stats["current_subject"] = info.name
            async with self.session_factory() as session:
                existing = {
                    (r.question_id, r.mode): r for r in (await session.exec(
                        select(QuestionSimilarityList).where(QuestionSimilarityList.subject_key == info.key)
                    )).all()
                }
            failed_before = stats["lists_failed"]
            todo = []
            for qid in info.published_ids:
                for mode in MODES:
                    row = existing.get((qid, mode))
                    if row is not None and row.algo_version == version and row.subject_signature == info.signature:
                        stats["lists_reused"] += 1  # finished by an earlier (interrupted) run
                    else:
                        todo.append((qid, mode))
            semaphore = asyncio.Semaphore(self.list_concurrency)

            async def compute(qid: int, mode: str) -> None:
                async with semaphore, self.session_factory() as session:
                    service = self._make_service(session, use_reranker)
                    try:
                        matches = await service.find_similar_questions(
                            qid, limit=stored_size, include_question=False, other_years_only=(mode == "other_years"),
                        )
                    except ProviderError as err:
                        if err.code.endswith(FATAL_PROVIDER_CODES_SUFFIX):
                            raise  # account / key / network problem: stop the run
                        # A problem with this one question (e.g. HTTP 400): skip it, students get live search
                        stats["lists_failed"] += 1
                        if len(stats["failed_examples"]) < 10:
                            stats["failed_examples"].append({"question_id": qid, "mode": mode, "code": err.code})
                        return
                    items = [
                        {"id": int(m["question_id"]), "score": int(m["similarity_score"]),
                         "raw": float(m.get("raw_score", 0.0)), "reasons": list(m.get("match_reasons", [])),
                         "ranking": m.get("ranking") or ranking_name(use_reranker)}
                        for m in matches
                    ]
                    await _upsert_list(session, QuestionSimilarityList(
                        question_id=qid, mode=mode, subject_key=info.key, subject_signature=info.signature,
                        algo_version=version, items=items, complete=len(items) < stored_size, computed_at=_now(),
                    ))
                    await session.commit()
                stats["lists_computed"] += 1

            # Chunks keep the heartbeat and progress fresh; a provider error cancels the rest of the chunk
            for start in range(0, len(todo), PROGRESS_EVERY_LISTS):
                async with asyncio.TaskGroup() as group:
                    for qid, mode in todo[start:start + PROGRESS_EVERY_LISTS]:
                        group.create_task(compute(qid, mode))
                await self._save(run_id, progress=progress, on_progress=on_progress)
            if stats["lists_failed"] > failed_before:
                # Leave the subject out of date so the next run retries the lists that failed
                stats["subjects_done"] += 1
                await self._save(run_id, progress=progress, on_progress=on_progress)
                continue
            async with self.session_factory() as session:
                state = await session.get(SimilaritySubjectState, info.key) or SimilaritySubjectState(subject_key=info.key)
                state.signature, state.algo_version = info.signature, version
                state.question_count, state.computed_at = len(info.published_ids), _now()
                session.add(state)
                await session.commit()
            stats["subjects_done"] += 1
            await self._save(run_id, progress=progress, on_progress=on_progress)
        stats.pop("current_subject", None)
        return stats

    async def _remove_stale_lists(self, session: AsyncSession, subjects: Dict[str, SubjectInfo]) -> int:
        """Lists of questions that are no longer published or moved to another subject; subjects that disappeared."""
        current = {qid: key for key, info in subjects.items() for qid in info.published_ids}
        rows = (await session.exec(select(QuestionSimilarityList.question_id, QuestionSimilarityList.subject_key))).all()
        stale_ids = sorted({qid for qid, key in rows if current.get(qid) != key})
        if stale_ids:
            await session.exec(delete(QuestionSimilarityList).where(col(QuestionSimilarityList.question_id).in_(stale_ids)))
        gone = [s.subject_key for s in (await session.exec(select(SimilaritySubjectState))).all() if s.subject_key not in subjects]
        if gone:
            await session.exec(delete(SimilaritySubjectState).where(col(SimilaritySubjectState.subject_key).in_(gone)))
        await session.commit()
        return len(stale_ids)

    # -- persistence ----------------------------------------------------------

    async def _save(self, run_id, phase: Optional[str] = None, progress=None, on_progress=None) -> None:
        async with self.session_factory() as session:
            run = await session.get(SearchIndexRun, run_id)
            if phase is not None:
                run.phase = phase
            if progress is not None:
                run.progress = json.loads(json.dumps(progress))  # new object so the JSON column is saved
            run.heartbeat_at = _now()
            session.add(run)
            await session.commit()
            await session.refresh(run)
        if on_progress is not None:
            await on_progress(run)

    async def _finish(self, run_id, status, progress, summary=None, error: Optional[Tuple[str, str]] = None,
                      on_progress=None) -> SearchIndexRun:
        async with self.session_factory() as session:
            run = await session.get(SearchIndexRun, run_id)
            run.status = status
            run.progress = json.loads(json.dumps(progress))
            run.summary = summary if summary is not None else _summary(progress)
            if status != STATUS_FAILED:
                run.phase = "done"
            if error is not None:
                run.error_code, run.error_message = error
            run.finished_at = run.heartbeat_at = _now()
            session.add(run)
            await session.commit()
            await session.refresh(run)
        if on_progress is not None:
            await on_progress(run)
        return run


async def _upsert_list(session: AsyncSession, row: QuestionSimilarityList) -> None:
    existing = await session.get(QuestionSimilarityList, (row.question_id, row.mode))
    if existing is None:
        session.add(row)
        return
    for attr in ("subject_key", "subject_signature", "algo_version", "items", "complete", "computed_at"):
        setattr(existing, attr, getattr(row, attr))
    session.add(existing)


def _summary(progress: Dict[str, Any]) -> Dict[str, Any]:
    emb = progress.get("embeddings", {}) or {}
    lists = progress.get("similar_lists", {}) or {}
    return {
        **({"ranking": lists["ranking"]} if lists.get("ranking") else {}),
        "questions_embedded": emb.get("question_embedded", 0),
        "solutions_embedded": emb.get("solution_embedded", 0),
        "embedding_failures": emb.get("question_failed", 0) + emb.get("solution_failed", 0),
        "subjects_updated": lists.get("subjects_done", 0),
        "lists_computed": lists.get("lists_computed", 0),
        "lists_reused": lists.get("lists_reused", 0),
        "lists_removed": lists.get("lists_removed", 0),
        "lists_failed": lists.get("lists_failed", 0),
    }


async def index_status(session_factory, use_reranker: bool = False) -> Dict[str, Any]:
    """Everything the admin page shows: what a vectors run and a similar-lists run (for the chosen ranking)
    would do and cost, the active run and recent runs."""
    runner = SearchIndexRunner(session_factory=session_factory)
    async with session_factory() as session:
        active = await runner.active_run(session)
        recent = (await session.exec(select(SearchIndexRun).order_by(col(SearchIndexRun.id).desc()).limit(10))).all()
        last_success = (await session.exec(
            select(SearchIndexRun).where(col(SearchIndexRun.status).in_((STATUS_SUCCEEDED, STATUS_WARNING)))
            .order_by(col(SearchIndexRun.id).desc()).limit(1)
        )).first()
    vectors_plan = await build_plan(session_factory, kind=KIND_VECTORS)
    lists_plan = await build_plan(session_factory, kind=KIND_LISTS, use_reranker=use_reranker)
    s = get_settings()
    return {
        "vectors_plan": vectors_plan,
        "lists_plan": lists_plan,
        "plan": lists_plan,  # kept for older clients
        "use_reranker": use_reranker,
        "active_run": active,
        "last_success_at": last_success.finished_at if last_success else None,
        "recent_runs": recent,
        "use_precomputed": s.SIMILARITY_USE_PRECOMPUTED,
        "live_use_reranker": s.SIMILARITY_LIVE_USE_RERANKER,
        "gemini_key_configured": bool(s.GEMINI_API_KEY),
        "jina_key_configured": bool(s.JINA_API_KEY),
    }
