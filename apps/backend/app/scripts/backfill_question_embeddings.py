"""
Backfill script for the hybrid similar-question search representations.

Per question it stores:
1. Question dense embedding      (Gemini, question text)
2. Question tsvector             (PostgreSQL 'english' full text, ranked with BM25)
3. Correct answer tsvector       (PostgreSQL 'english' full text, ranked with BM25)
4. Explanation dense embedding   (Gemini, English solution text)
and then refreshes the BM25 corpus statistics.

Only what changed is re-embedded: the question vector when the question text changes, the explanation
vector when the solution changes. The correct-option text is no longer embedded (one-word and
"1 and 2 only" answers made that vector noise); it is still indexed for BM25.

This script runs a tracked **vectors** run (see app/services/vector/search_index.py): embeddings, then
BM25 statistics. It does NOT build the similar-question lists any more; once the vectors exist, build
(and rebuild, with or without the Jina reranker) the lists with:
  python -m app.scripts.build_similar_lists            # fused ranking (RRF), no paid API calls
  python -m app.scripts.build_similar_lists --reranker # Jina reranker
Each run is recorded in search_index_runs and shown in Admin → Similar Questions; a run stopped by an
error (no credits, quota, bad key) resumes where it stopped the next time it is started.

Usage:
  # Dry run: what would be done and the estimated Gemini usage
  python -m app.scripts.backfill_question_embeddings --dry-run

  # Vectorize first 10 questions for testing
  python -m app.scripts.backfill_question_embeddings --limit 10

  # Vectorize all questions
  python -m app.scripts.backfill_question_embeddings --all

  # Vectorize a specific exam
  python -m app.scripts.backfill_question_embeddings --exam-id 12

  # Only recompute BM25 statistics
  python -m app.scripts.backfill_question_embeddings --refresh-bm25-only

  # --skip-similar is still accepted (it is now always the case)

Embeddings use Gemini's batchEmbedContents (up to 100 texts per request); a failed batch is retried
text by text, and anything still failing is picked up by the next run (hash check).
"""
import sys
import argparse
import asyncio
from pathlib import Path
from typing import List, Dict, Any, Optional

# Ensure apps/backend is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from sqlalchemy import text
from sqlmodel import select
from app.db.session import async_session_factory
from app.db.models.question import Question, QuestionTranslation, QuestionOption, OptionTranslation
from app.db.models.explanation import QuestionExplanation
from app.db.models.question_embedding import QuestionEmbedding
from app.db.models.enums import QuestionStatus
from app.db.models.enums import LanguageCode
from app.services.vector.gemini_embedder import GeminiEmbedder, compute_sha256
from app.services.vector.bm25 import refresh_bm25_stats
from app.config.logger import setup_logger
from app.config.settings import get_settings

logger = setup_logger("BackfillQuestionEmbeddings")

# gemini-embedding-001 accepts ~2048 tokens; solutions are a paragraph or two, this only guards outliers
MAX_EXPLANATION_CHARS = 8000


def explanation_text_for_embedding(solution: Optional[str]) -> Optional[str]:
    clean = (solution or "").strip()
    return clean[:MAX_EXPLANATION_CHARS] if clean else None


async def fetch_question_candidates(
    exam_id: Optional[int] = None,
    limit: Optional[int] = None,
    force: bool = False,
    session_factory=None,
) -> List[Dict[str, Any]]:
    """Fetch questions with their English stem, correct answer and solution, plus what needs (re)embedding."""
    async with (session_factory or async_session_factory)() as session:
        # 1. Fetch questions
        stmt = select(Question.id, Question.exam_id, Question.status).order_by(Question.id)
        if exam_id:
            stmt = stmt.where(Question.exam_id == exam_id)
        if limit:
            stmt = stmt.limit(limit)

        q_rows = (await session.exec(stmt)).all()
        q_ids = [r[0] for r in q_rows]
        published = {r[0] for r in q_rows if r[2] == QuestionStatus.PUBLISHED}
        if not q_ids:
            return []

        # 2. Fetch question texts (prefer English)
        trans_stmt = select(
            QuestionTranslation.question_id,
            QuestionTranslation.language,
            QuestionTranslation.question_text,
        ).where(QuestionTranslation.question_id.in_(q_ids))
        trans_rows = (await session.exec(trans_stmt)).all()

        q_texts: Dict[int, str] = {}
        for qid, lang, txt in trans_rows:
            if qid not in q_texts or lang == LanguageCode.ENGLISH:
                q_texts[qid] = txt

        # 3. Fetch correct answer texts (options with is_correct = True)
        opt_stmt = (
            select(
                QuestionOption.question_id,
                QuestionOption.id,
                OptionTranslation.language,
                OptionTranslation.option_text,
            )
            .join(OptionTranslation, OptionTranslation.option_id == QuestionOption.id)
            .where(
                QuestionOption.question_id.in_(q_ids),
                QuestionOption.is_correct.is_(True),
            )
        )
        opt_rows = (await session.exec(opt_stmt)).all()

        ans_texts: Dict[int, str] = {}
        for qid, opt_id, lang, txt in opt_rows:
            if qid not in ans_texts or lang == LanguageCode.ENGLISH:
                ans_texts[qid] = txt

        # 4. Fetch solution texts (prefer English)
        expl_rows = (await session.exec(
            select(QuestionExplanation.question_id, QuestionExplanation.language, QuestionExplanation.detailed_solution)
            .where(QuestionExplanation.question_id.in_(q_ids))
        )).all()
        expl_texts: Dict[int, str] = {}
        for qid, lang, txt in expl_rows:
            if txt and (qid not in expl_texts or lang == LanguageCode.ENGLISH):
                expl_texts[qid] = txt

        # 5. Check existing embeddings and hashes
        existing_rows = (await session.exec(
            select(
                QuestionEmbedding.question_id,
                QuestionEmbedding.question_hash,
                QuestionEmbedding.answer_hash,
                QuestionEmbedding.explanation_hash,
                QuestionEmbedding.question_dense_embedding.is_not(None),
                QuestionEmbedding.explanation_dense_embedding.is_not(None),
            ).where(QuestionEmbedding.question_id.in_(q_ids))
        )).all()
        existing = {row[0]: tuple(row[1:]) for row in existing_rows}

        candidates = []
        for qid in q_ids:
            q_txt = q_texts.get(qid, "").strip()
            ans_txt = ans_texts.get(qid, "").strip() if ans_texts.get(qid) else None
            expl_txt = explanation_text_for_embedding(expl_texts.get(qid))
            q_hash = compute_sha256(q_txt)
            ans_hash = compute_sha256(ans_txt)
            expl_hash = compute_sha256(expl_txt)

            ex = existing.get(qid)
            if ex:
                ex_q_hash, ex_ans_hash, ex_expl_hash, has_q_vec, has_expl_vec = ex
                embed_question = force or ex_q_hash != q_hash or not has_q_vec
                embed_explanation = force or ex_expl_hash != expl_hash or (bool(expl_txt) and not has_expl_vec)
                is_uptodate = not embed_question and not embed_explanation and ex_ans_hash == ans_hash
            else:
                embed_question = embed_explanation = True
                is_uptodate = False

            candidates.append({
                "question_id": qid,
                "published": qid in published,
                "question_text": q_txt,
                "answer_text": ans_txt,
                "explanation_text": expl_txt,
                "question_hash": q_hash,
                "answer_hash": ans_hash,
                "explanation_hash": expl_hash,
                "embed_question": embed_question,
                "embed_explanation": embed_explanation,
                "is_uptodate": is_uptodate,
            })

        return candidates


_UPSERT_SQL = text("""
    INSERT INTO question_embeddings (
        question_id,
        question_dense_embedding,
        question_tsv,
        answer_tsv,
        explanation_dense_embedding,
        question_hash,
        answer_hash,
        explanation_hash,
        embedding_model,
        updated_at
    ) VALUES (
        :qid,
        CAST(:q_vec AS halfvec),
        to_tsvector('english', coalesce(CAST(:q_txt AS text), '')),
        CASE WHEN CAST(:ans_txt AS text) IS NOT NULL AND length(CAST(:ans_txt AS text)) > 0
             THEN to_tsvector('english', CAST(:ans_txt AS text))
             ELSE NULL
        END,
        CAST(:expl_vec AS halfvec),
        :q_hash,
        :ans_hash,
        :expl_hash,
        :model,
        NOW()
    )
    ON CONFLICT (question_id) DO UPDATE SET
        question_dense_embedding = CASE WHEN CAST(:set_q AS boolean)
            THEN EXCLUDED.question_dense_embedding ELSE question_embeddings.question_dense_embedding END,
        question_hash = CASE WHEN CAST(:set_q AS boolean)
            THEN EXCLUDED.question_hash ELSE question_embeddings.question_hash END,
        explanation_dense_embedding = CASE WHEN CAST(:set_expl AS boolean)
            THEN EXCLUDED.explanation_dense_embedding ELSE question_embeddings.explanation_dense_embedding END,
        explanation_hash = CASE WHEN CAST(:set_expl AS boolean)
            THEN EXCLUDED.explanation_hash ELSE question_embeddings.explanation_hash END,
        question_tsv = EXCLUDED.question_tsv,
        answer_tsv = EXCLUDED.answer_tsv,
        answer_hash = EXCLUDED.answer_hash,
        embedding_model = EXCLUDED.embedding_model,
        updated_at = NOW();
""")


def _vec_str(vec: Optional[List[float]]) -> Optional[str]:
    return f"[{','.join(str(x) for x in vec)}]" if vec else None


async def process_batch(
    batch: List[Dict[str, Any]],
    embedder: GeminiEmbedder,
    concurrency: int = 8,
    stats: Optional[Dict[str, int]] = None,
) -> int:
    """Embed what changed in a batch (question and/or solution) and upsert into question_embeddings.

    Texts go to Gemini's batch endpoint (up to 100 per request); `concurrency` only applies when a
    batch fails and its texts are retried one by one.
    """
    q_texts = [item["question_text"] if item.get("embed_question", True) else None for item in batch]
    expl_texts = [item.get("explanation_text") if item.get("embed_explanation", True) else None for item in batch]
    q_vectors, expl_vectors = await asyncio.gather(
        embedder.embed_texts_batch(q_texts, fallback_concurrency=concurrency),
        embedder.embed_texts_batch(expl_texts, fallback_concurrency=concurrency),
    )

    model_name = embedder.model
    saved = 0
    counts = {"question_embedded": 0, "question_failed": 0, "solution_embedded": 0, "solution_failed": 0, "no_text": 0}
    async with async_session_factory() as session:
        for item, q_vec, expl_vec in zip(batch, q_vectors, expl_vectors):
            set_q = bool(item.get("embed_question", True))
            set_expl = bool(item.get("embed_explanation", True))
            if set_q and not q_vec:
                if item.get("question_text"):
                    counts["question_failed"] += 1
                    logger.warning(f"Skipping Q#{item['question_id']}: failed to generate question vector.")
                else:
                    counts["no_text"] += 1
                continue
            if set_expl and item.get("explanation_text") and not expl_vec:
                # Keep the previous solution vector and hash, so the next run retries it
                counts["solution_failed"] += 1
                logger.warning(f"Q#{item['question_id']}: failed to embed the solution; will retry next run.")
                set_expl = False
            counts["question_embedded"] += int(set_q)
            counts["solution_embedded"] += int(set_expl and bool(expl_vec))

            await session.exec(
                _UPSERT_SQL,
                params={
                    "qid": item["question_id"],
                    "q_vec": _vec_str(q_vec),
                    "q_txt": item["question_text"],
                    "ans_txt": item["answer_text"],
                    "expl_vec": _vec_str(expl_vec),
                    "q_hash": item["question_hash"],
                    "ans_hash": item["answer_hash"],
                    "expl_hash": item.get("explanation_hash") if set_expl else None,
                    "set_q": set_q,
                    "set_expl": set_expl,
                    "model": model_name,
                },
            )
            saved += 1

        await session.commit()
    if stats is not None:
        for key, value in counts.items():
            stats[key] = stats.get(key, 0) + value
    return saved


async def refresh_bm25_statistics() -> Dict[str, int]:
    async with async_session_factory() as session:
        return await refresh_bm25_stats(session)


def _print_plan(plan: Dict[str, Any]) -> None:
    t, pend, est = plan["totals"], plan["pending"], plan["estimate"]
    kind = plan.get("kind", "full")
    print(f"\n--- Search index: what a {kind} run would do ---")
    print(f"Questions                  : {t['questions']} ({t['published_with_subject']} published with a subject, "
          f"{t['published_without_subject']} published without one)")
    by_rank = t.get("stored_lists_by_ranking") or {}
    print(f"Subjects                   : {t['subjects']}  (stored lists: RRF {by_rank.get('rrf', 0)}, "
          f"reranked {by_rank.get('rerank', 0)}, out of date {by_rank.get('outdated', 0)})")
    if kind in ("vectors", "full"):
        print(f"Question embeddings to make: {pend['question_embeddings']}")
        print(f"Solution embeddings to make: {pend['solution_embeddings']}")
        print(f"Estimated Gemini usage     : {est['gemini']['requests']} requests, ~{est['gemini']['approx_tokens']:,} tokens")
    if kind in ("similar_lists", "full"):
        print(f"Ranking                    : {'Jina reranker' if plan.get('ranking') == 'rerank' else 'fused ranking (RRF), no reranker'}")
        print(f"Subjects out of date       : {pend['subjects_out_of_date']}")
        print(f"Similar lists to compute   : {pend['lists_to_compute']}")
        print(f"Estimated Jina usage       : {est['jina']['rerank_calls']} rerank calls, ~{est['jina']['approx_tokens']:,} tokens")
        if kind == "similar_lists" and pend.get("embeddings_not_built"):
            print(f"WARNING                    : {pend['embeddings_not_built']} text(s) have no vector yet; run "
                  "`python -m app.scripts.backfill_question_embeddings` first for the best lists")
    print(f"Estimated time             : ~{est['approx_minutes']} min")
    print(f"Note: {est['note']}")
    print("------------------------------------------\n")


async def main():
    from app.services.vector.search_index import RunAlreadyActive, SearchIndexRunner, build_plan

    parser = argparse.ArgumentParser(
        description="Vectors run: embeddings (Gemini) and BM25 statistics. "
                    "Similar lists are built separately: python -m app.scripts.build_similar_lists"
    )
    parser.add_argument("--all", action="store_true", help="Process all questions (the default; kept for old commands)")
    parser.add_argument("--exam-id", type=int, default=None, help="Embed only this exam's questions")
    parser.add_argument("--limit", type=int, default=None, help="Embed at most N questions")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Questions per batch; each batch is one Gemini request for questions and one for solutions (default: 100)")
    parser.add_argument("--force", action="store_true", help="Re-embed even if the text is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="Show what a run would do and its estimated API usage; change nothing")
    parser.add_argument("--refresh-bm25-only", action="store_true", help="Only recompute BM25 statistics")
    parser.add_argument("--skip-similar", action="store_true",
                        help="Accepted for old commands; this script never builds similar lists now")
    args = parser.parse_args()

    if args.refresh_bm25_only:
        print(f"BM25 statistics refreshed: {await refresh_bm25_statistics()}")
        return

    plan = await build_plan(async_session_factory, exam_id=args.exam_id, force=args.force, kind="vectors")
    _print_plan(plan)
    if args.dry_run:
        print("[dry-run] No changes made.")
        return
    if plan["up_to_date"] and not args.force:
        print("Everything is up to date; refreshing BM25 statistics only.")

    async def on_progress(run) -> None:
        p = run.progress or {}
        if run.phase == "embeddings" and p.get("embeddings"):
            e = p["embeddings"]
            print(f"  embeddings: {e.get('processed', 0)}/{e.get('to_process', 0)} questions")

    options = {"kind": "vectors", "exam_id": args.exam_id, "limit": args.limit, "force": args.force,
               "batch_size": args.batch_size}
    try:
        run = await SearchIndexRunner().run("cli", options=options, on_progress=on_progress)
    except RunAlreadyActive as busy:
        print(f"ERROR: {busy}. Wait for it to finish (Admin → Similar Questions shows its progress).")
        sys.exit(2)

    print(f"\nRun #{run.id}: {run.status.upper()}")
    for key, value in (run.summary or {}).items():
        print(f"  {key}: {value}")
    if run.error_code:
        print(f"  reason [{run.error_code}]: {run.error_message}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
