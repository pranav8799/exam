"""
Background task for vectorizing exam questions into the 4-channel representation
(Question dense, Question lexical, Answer dense, Answer lexical) after PDF extraction.
"""
import asyncio
from app.config.logger import setup_logger
from app.tasks.celery_app import celery_app

logger = setup_logger("VectorizationTasks")


@celery_app.task(name="vectorize_exam_questions_task")
def vectorize_exam_questions_task(exam_id_str: str):
    """Celery entry point for vectorizing questions of one exam."""
    return asyncio.run(async_vectorize_exam(exam_id_str))


async def async_vectorize_exam(exam_id_str: str) -> dict:
    """After a PDF upload: a tracked vectors run (embeddings for this paper, BM25 statistics). Similar
    lists are left to an admin's similar-lists run. Never raises; the result is also on the run row
    (Admin → Similar Questions). If another run is in progress, only this paper's embeddings are
    made now and its subjects stay "out of date" until the next run."""
    from app.services.vector.search_index import RunAlreadyActive, SearchIndexRunner

    exam_id = int(exam_id_str)
    try:
        logger.info(f"🚀 Starting search-index run for Exam #{exam_id}...")
        try:
            # Vectors only (embeddings + BM25). Similar lists are rebuilt by an admin's similar-lists run,
            # with or without the reranker; until then this paper's questions are searched live.
            run = await SearchIndexRunner().run("upload", options={"exam_id": exam_id, "kind": "vectors"})
        except RunAlreadyActive as busy:
            logger.info(f"Search-index run #{busy.run_id} is active; embedding Exam #{exam_id} only.")
            return await _embed_exam_only(exam_id)
        emb = (run.progress or {}).get("embeddings", {})
        logger.info(f"Search-index run #{run.id} for Exam #{exam_id} ended: {run.status} {run.error_code or ''}")
        return {
            "exam_id": exam_id,
            "run_id": run.id,
            "vectorized": emb.get("question_embedded", 0),
            "status": "FAILED" if run.status == "failed" else ("UP_TO_DATE" if not emb.get("to_process") else "SUCCESS"),
            **({"error": run.error_message} if run.error_message else {}),
        }
    except Exception as err:
        logger.error(f"❌ Error vectorizing questions for Exam #{exam_id}: {err}", exc_info=True)
        return {"exam_id": exam_id, "error": str(err), "status": "FAILED"}


async def _embed_exam_only(exam_id: int) -> dict:
    from app.scripts.backfill_question_embeddings import fetch_question_candidates, process_batch
    from app.services.vector.gemini_embedder import GeminiEmbedder

    candidates = await fetch_question_candidates(exam_id=exam_id)
    to_process = [c for c in candidates if not c.get("is_uptodate")]
    if not to_process:
        return {"exam_id": exam_id, "vectorized": 0, "status": "UP_TO_DATE"}
    saved = await process_batch(to_process, GeminiEmbedder(), concurrency=8)
    return {"exam_id": exam_id, "vectorized": saved, "status": "SUCCESS"}


def dispatch_exam_vectorization(exam_id: int, log=logger) -> None:
    """Queue vectorization on a Celery worker when running, otherwise as an asyncio background task."""
    try:
        from app.tasks.celery_app import has_active_celery_workers

        if has_active_celery_workers():
            vectorize_exam_questions_task.delay(str(exam_id))
            log.info(f"🧠 Dispatched 4-channel vectorization to Celery for Exam #{exam_id}.")
        else:
            asyncio.create_task(async_vectorize_exam(str(exam_id)))
            log.info(f"🧠 Dispatched 4-channel vectorization via asyncio for Exam #{exam_id}.")
    except Exception as err:
        log.warning(f"Could not trigger vectorization for Exam #{exam_id}: {err}")

