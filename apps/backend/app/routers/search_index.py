"""Admin → Similar Questions → Search index: state before a run, start/resume a run, run history.

All endpoints need the "Similar questions" admin section. A run executes in the API process as a
background task and records its progress, so the page can poll it; one run at a time (409 otherwise).
"""
import asyncio
from typing import Any, Dict, Literal, Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlmodel import col, select

from app.config.logger import setup_logger
from app.core.deps import require_admin_section
from app.db.models.search_index import SearchIndexRun
from app.db.models.user import User

logger = setup_logger("SearchIndexAPI")

router = APIRouter(prefix="/admin/search-index", tags=["Admin: Search index"])

SECTION = "admin.similar_questions"

# Keeps references to running tasks (asyncio only keeps weak ones)
_running_tasks: Set[asyncio.Task] = set()


def _session_factory():
    from app.db.session import async_session_factory
    return async_session_factory


def run_to_dict(run: Optional[SearchIndexRun]) -> Optional[Dict[str, Any]]:
    if run is None:
        return None
    iso = lambda d: d.isoformat() if d else None  # noqa: E731
    return {
        "id": run.id,
        "status": run.status,
        "trigger": run.trigger,
        "triggered_by_id": run.triggered_by_id,
        "resumed_from_id": run.resumed_from_id,
        "phase": run.phase,
        "options": run.options or {},
        "estimate": run.estimate or {},
        "progress": run.progress or {},
        "summary": run.summary or {},
        "error_code": run.error_code,
        "error_message": run.error_message,
        "created_at": iso(run.created_at),
        "started_at": iso(run.started_at),
        "heartbeat_at": iso(run.heartbeat_at),
        "finished_at": iso(run.finished_at),
    }


class StartRunPayload(BaseModel):
    kind: Optional[Literal["vectors", "similar_lists", "full"]] = Field(
        default=None,
        description='"vectors": embeddings + BM25 (Gemini). "similar_lists": the precomputed lists, from the '
                    'vectors already built. Omitted: "full" (all phases, the original behaviour).',
    )
    use_reranker: bool = Field(default=False, description="Similar lists: rerank with Jina (else fused RRF order)")
    force: bool = Field(default=False, description="Re-embed every question even if its text is unchanged (costly)")
    skip_similar: bool = Field(default=False, description="Only embeddings and BM25, no similar lists")


@router.get("/status")
async def get_status(
    use_reranker: bool = Query(default=False, description="Estimate the similar-lists run with the Jina reranker"),
    _: User = Depends(require_admin_section(SECTION)),
):
    """State before a run: pending work, estimated API usage, the run in progress and recent runs."""
    from app.services.vector.search_index import index_status

    data = await index_status(_session_factory(), use_reranker=use_reranker)
    data["active_run"] = run_to_dict(data["active_run"])
    data["recent_runs"] = [run_to_dict(r) for r in data["recent_runs"]]
    data["last_success_at"] = data["last_success_at"].isoformat() if data["last_success_at"] else None
    return data


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    payload: StartRunPayload,
    current_user: User = Depends(require_admin_section(SECTION)),
):
    """Start a run (it resumes after a failed one: finished work is not repeated)."""
    from app.config.settings import get_settings
    from app.services.vector.search_index import RunAlreadyActive, SearchIndexRunner

    kind = payload.kind or "full"
    use_reranker = payload.use_reranker and kind != "vectors"
    if use_reranker and not get_settings().JINA_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JINA_API_KEY is not set in the backend .env, so the reranker cannot be used. "
                   "Start the run without the reranker, or add the key and restart the API.",
        )
    runner = SearchIndexRunner(session_factory=_session_factory())
    try:
        run = await runner.create_run(
            "admin", user_id=current_user.id,
            options={"kind": kind, "use_reranker": use_reranker,
                     "force": payload.force, "skip_similar": payload.skip_similar},
        )
    except RunAlreadyActive as busy:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"message": str(busy), "run_id": busy.run_id})
    task = asyncio.create_task(runner.execute(run.id))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)
    logger.info(f"Search-index run #{run.id} started by user #{current_user.id}")
    return run_to_dict(run)


@router.get("/runs")
async def list_runs(
    limit: int = Query(default=20, ge=1, le=100),
    _: User = Depends(require_admin_section(SECTION)),
):
    async with _session_factory()() as session:
        runs = (await session.exec(
            select(SearchIndexRun).order_by(col(SearchIndexRun.id).desc()).limit(limit)
        )).all()
    return [run_to_dict(r) for r in runs]


@router.get("/runs/{run_id}")
async def get_run(run_id: int, _: User = Depends(require_admin_section(SECTION))):
    async with _session_factory()() as session:
        run = await session.get(SearchIndexRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return run_to_dict(run)
