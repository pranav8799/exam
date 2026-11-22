"""
Build the precomputed similar-question lists from the vectors that already exist.

This is the second half of the search index, run separately from the vectors
(`python -m app.scripts.backfill_question_embeddings`), so lists can be rebuilt as often as needed,
with or without the Jina reranker, without paying for embeddings again. It is the same as
Admin → Similar Questions → "Build similar lists".

  Without the reranker (default): the order and the displayed score come from the fused ranking
  (weighted RRF over the five channels). No paid API calls.
  With --reranker: the top fused candidates are reranked by Jina (needs JINA_API_KEY; paid, paced to
  the key's limits). Stops cleanly on no credits / quota and resumes on the next start.

Switching between the two rebuilds every subject (the ranking is part of the algorithm version). Until a
subject is rebuilt, students keep seeing its existing lists.

Usage (from apps/backend):
  python -m app.scripts.build_similar_lists --dry-run            # what would be computed, and the estimate
  python -m app.scripts.build_similar_lists                      # all out-of-date subjects, no reranker
  python -m app.scripts.build_similar_lists --reranker           # all out-of-date subjects, Jina reranker
  python -m app.scripts.build_similar_lists --subject Physics    # one subject (repeatable), e.g. a trial
"""
import argparse
import asyncio
import sys

from app.db.session import async_session_factory
from app.scripts.backfill_question_embeddings import _print_plan


async def main() -> None:
    from app.config.settings import get_settings
    from app.services.vector.search_index import RunAlreadyActive, SearchIndexRunner, build_plan

    parser = argparse.ArgumentParser(description="Build the precomputed similar-question lists from existing vectors")
    parser.add_argument("--reranker", action="store_true", help="Rerank with Jina (default: fused RRF ranking only)")
    parser.add_argument("--subject", action="append", default=None,
                        help="Only this subject (repeatable); e.g. a trial run")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be computed; change nothing")
    args = parser.parse_args()

    if args.reranker and not get_settings().JINA_API_KEY:
        print("ERROR: --reranker needs JINA_API_KEY in the backend .env.")
        sys.exit(2)

    plan = await build_plan(async_session_factory, use_reranker=args.reranker, kind="similar_lists")
    _print_plan(plan)
    if args.dry_run:
        print("[dry-run] No changes made.")
        return

    async def on_progress(run) -> None:
        sl = (run.progress or {}).get("similar_lists")
        if run.phase == "similar_lists" and sl:
            print(f"  similar lists: subjects {sl.get('subjects_done', 0)}/{sl.get('subjects_to_update', 0)}, "
                  f"lists {sl.get('lists_computed', 0) + sl.get('lists_reused', 0)}/{sl.get('lists_total', 0)}"
                  f"  {sl.get('current_subject', '')}")

    options = {"kind": "similar_lists", "use_reranker": args.reranker, "subjects": args.subject or []}
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
