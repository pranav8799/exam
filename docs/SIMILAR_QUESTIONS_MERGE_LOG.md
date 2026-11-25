# Similar Questions — Merge and Change Log

A running record of bringing the similar-questions feature from the `atharvadev` branch copy
(`atharvadevbranchcode/EXAM-PLANNER-atharvadev`) into this codebase, and of every change made on top of it:
what changed, where, and why. Newest entries are added at the bottom of each section.

The feature itself is documented in [SIMILAR_QUESTIONS.md](SIMILAR_QUESTIONS.md).

---

## 1. Decisions (from the product owner, 2026-11-22)

| # | Decision | Why |
|---|---|---|
| D1 | Bring in the similarity feature from the `atharvadev` copy. | It is the finished, verified backend plus its UI. |
| D2 | **The Jina reranker is optional.** Without it, the order and the displayed score come straight from the fusion ranking (weighted RRF). | No reranker for now; keep the option to try it later. |
| D3 | **The admin chooses, per run, whether a similar-lists run uses the reranker.** | To compare results with and without Jina on the same vectors. |
| D4 | **Building vectors and building similar lists are separate runs** (separate admin buttons and separate script commands). | Vectors (Gemini) are built once; similar lists can then be rebuilt as often as needed, with or without Jina, without paying for embeddings again. |
| D5 | **Students see 15 similar questions** (Admin → Similar Questions). | Product choice. |
| D6 | Log every change in this file. | Traceability. |
| D7 | (2026-11-25) **The practice panel's exam toggles start with only the current question's exam on**; the student switches other exams on. Not a bug. | Product choice: a UPSC student sees UPSC first. |
| D8 | (2026-11-25) The panel title stays **"Cross Exam Similar Questions"**. | Product choice. |
| D9 | (2026-11-25) **A question without a subject shows a blank subject**, never a made-up "General Studies". | Keep the UI simple: show the subject if it exists, otherwise nothing. |

---

## 2. Environment and setup steps (local machine)

| When | Step | Why |
|---|---|---|
| 2026-11-22 | Installed Visual Studio 2022 Build Tools (C++ workload, MSVC 14.44) with `winget`. | pgvector has no official Windows binaries; it must be compiled. |
| 2026-11-22 | Built pgvector **0.8.7** from the official source (`github.com/pgvector/pgvector`, tag `v0.8.7`) with `nmake /F Makefile.win` and installed it into PostgreSQL 18 (`lib\vector.dll`, `share\extension\vector*`, `include\server\extension\vector`). | Migration 073 needs the `vector` extension (≥ 0.7 for `halfvec`). |
| 2026-11-22 | Installed GitNexus (`npx gitnexus analyze`); added `.gitnexusignore` so the branch copy and `.db_backups/` are not indexed. | Code-graph analysis before edits (see `CLAUDE.md`). |
| 2026-11-22 | Work done on git branch `feature/similar-questions` (from `main`). | Keep `main` unchanged until reviewed. |
| 2026-11-22 | Full database backup before any migration: `.db_backups/Exam_planner_19112026_before_similar_questions_2026-11-22.dump` (`pg_dump -F c`, 32 MB). `.db_backups/` is excluded from git (`.git/info/exclude`). | Migrations 073–077 change the schema, and 076 changes data. Restore with `pg_restore --clean -d <db> <file>`. |

---

## 3. Files brought in from the branch copy

**How the copy was compared.** The copy has no git history, so every file of both trees was compared by
content (line endings ignored; `node_modules`, `.venv`, caches and build output skipped). Result: our code
was exactly the branch's starting point. Nothing existed only in our code except local files
(`.gitnexusignore`, `AGENTS.md`, this log, SQLite leftovers), so copying the branch files lost none of our work.
Every changed file's diff was read before copying.

**Impact analysis (GitNexus, before copying).** HIGH risk was reported for three symbols:
- `async_process_exam_pdf` (PDF upload, retry and restart of the pipeline): the branch adds one call at the end
  that starts building vectors. That call catches its own errors, so a failure cannot break an upload.
- `RecommendedQuestionCard` and `useSimilarQuestions`: also used by `AnalyticsPage` and `PsychometricReportView`
  (post-exam report). The frontend type-check passes with the new versions.

### 3.1 Copied as they are (56 files)

| Area | Files |
|---|---|
| Migrations (new) | `apps/backend/alembic/versions/073_add_question_embeddings.py`, `074_explanation_embedding_and_bm25.py`, `075_similar_questions_limit_setting.py`, `076_clean_old_pipeline_hierarchy_links.py`, `077_precomputed_similar_questions.py` |
| Backend (new) | `app/core/subjects.py`, `app/db/models/question_embedding.py`, `app/db/models/search_index.py`, `app/routers/search_index.py`, `app/scripts/audit_similar_questions.py`, `app/scripts/backfill_question_embeddings.py`, `app/services/vector/{bm25,gemini_embedder,jina_reranker,provider_errors,rate_limiter,search_index,similarity_retrieval_service,stored_similarities}.py`, `app/tasks/vector_tasks.py` |
| Backend (changed) | `app/config/settings.py`, `app/core/deps.py`, `app/db/models/__init__.py`, `app/db/models/site_settings.py`, `app/fastapi_server.py`, `app/repositories/question_repository.py`, `app/routers/questions.py`, `app/routers/site_settings.py`, `app/schemas/site_settings.py`, `app/services/exam_json_builder.py`, `app/services/site_settings_service.py`, `app/tasks/exam_tasks.py`, `pyproject.toml`, `run_migration.py` |
| Backend tests | `tests/test_search_index.py`, `tests/test_similarity_helpers.py`, `tests/test_subject_filter.py` (new), `tests/test_pyq_similar_and_sets.py` (changed) |
| Frontend (new) | `components/admin/QuestionSimilarExplorer.tsx`, `components/admin/views/SearchIndexPanel.tsx`, `components/admin/views/SimilarQuestionsSettingsView.tsx`, `services/searchIndexService.ts` |
| Frontend (changed) | `components/admin/{AdminQuickSearch,AdminSidebar,QuestionBankManager}.tsx`, `components/admin/views/QuestionReviewView.tsx`, `components/exam/{RecommendedQuestionCard,SimilarQuestionsSection}.tsx`, `constants/adminAccess.ts`, `pages/{AdminPage,GuestExamPage,StudentPracticeExamPage}.tsx`, `services/{similarQuestionService,siteSettingsService}.ts` |
| Root / docs | `pyproject.toml` (adds `pgvector`), `docs/SIMILAR_QUESTIONS.md` |

`uv sync` installed the `pgvector` Python package (0.5.1) and updated `uv.lock`.

### 3.2 Deliberately NOT brought in

| File | Why |
|---|---|
| `apps/backend/app/engine/post_submission_pipeline.py` | **The branch's version has a bug.** Its only change replaces `q_map.values()` with `q_rows`, but `q_rows` is assigned only inside an `if` a few lines above, so whenever that `if` is skipped the report pipeline would crash with `UnboundLocalError`. Our line is correct and unrelated to similar questions. Kept ours. |
| `scripts/kill-ports.ps1` + the `clean:ports` / `kill:ports` scripts in the root `package.json` | Developer convenience, not part of the feature. The script force-stops **any** program listening on ports 8000/3000/4321, not only this project's servers. |
| `.github/workflows/docker-publish.yml` | CI workflow present in the copy but not in this repository; outside the feature. Not added without a separate decision. |
| `CLAUDE.md` from the copy | Contains the copy's GitNexus index name and counts; ours is maintained by our own GitNexus index. |
| `package-lock.json`, `uv.lock` from the copy | Regenerated locally by `npm install` / `uv sync` instead. |

---

## 4. Changes made on top of the branch code

### 4.1 The Jina reranker is optional; RRF score when it is off (D2)

| File | Change |
|---|---|
| `app/config/settings.py` | New `SIMILARITY_LIVE_USE_RERANKER` (default **false**): whether **live** searches (questions without a stored list) use the reranker. Runs choose for themselves. |
| `app/services/vector/similarity_retrieval_service.py` | `HybridSimilarityService(..., use_reranker=None)` (None = the setting above). The Jina client is only created when used, so a missing key is not reported when the reranker is off. Without the reranker, the five channels and the weighted RRF run exactly as before, and the top results of the fused ranking are returned directly. |
| same file, new `rrf_display_score()` | The displayed percentage without the reranker. Let `max` be the RRF score a question would get by ranking 1st in every channel that ran (sum of the weights ÷ (k + 1)); then `score = round(40 + 59 × rrf ÷ max)`, limited to 40–99. Examples with all five channels: 1st everywhere → 99; 10th everywhere → 91; 50th everywhere → 73; 1st only in the 0.30-weight question-vector channel → 58. `tier` stays "exact" at ≥ 85. Using the channels that actually ran keeps questions with "code" answers (answer channels skipped) from being penalised. |
| same file, `build_result_cards` | Each result carries `ranking`: `"rrf"` or `"rerank"`. |
| `components/admin/QuestionSimilarExplorer.tsx` | Shows an "RRF ranked" / "Jina reranked" badge per result. Channel badges no longer show hard-coded weights ("Q-Dense (30%)" → "Q-Dense"); the weights are server settings and the old labels went wrong when they changed (branch open item 8.1 #4). |

Why the RRF score had to be new: without the reranker, the branch code fell back to placeholder scores
(1.00, 0.99, 0.98… by position), which the reranker formula turned into 97–99% "exact" for every result.

### 4.2 Vectors and similar lists are separate runs; reranker chosen per run (D3, D4)

| File | Change |
|---|---|
| `app/services/vector/search_index.py` | A run has a **kind** (`options["kind"]`): `"vectors"` = embeddings + BM25 statistics; `"similar_lists"` = BM25 statistics (free, from the stored tsvectors) + lists; `"full"` = all phases (the old behaviour, still used when no kind is given). `options["use_reranker"]` picks the ranking for the lists. |
| same file, `algo_version(use_reranker)` | The algorithm version now includes the ranking (`"rrf"` / `"rerank"`); reranker settings (`SIMILARITY_RERANK_TOP_N`, `JINA_RERANKER_MODEL`) only count for reranked lists. So switching ranking makes every subject out of date for the next run, while changing a reranker setting no longer invalidates RRF lists. New `current_versions()` returns both valid versions. |
| same file, `build_plan(..., use_reranker, kind)` | Estimates per kind: Gemini only for vectors, Jina only for reranked lists, separate time estimates (`estimate.minutes.vectors` / `.similar_lists`), stored lists counted per ranking (`totals.stored_lists_by_ranking`), and `pending.embeddings_not_built` so a lists run can warn that some vectors are missing. |
| same file, runner | A lists run with the reranker but no `JINA_API_KEY` stops with `jina_no_key` **before** deleting or recomputing anything. Stored list items record their `ranking`. Run summary records `ranking`. |
| `app/services/vector/stored_similarities.py` | `read_stored` accepts a list built with **either** ranking (as long as it matches the current settings), so students always see whichever lists were built last; switching ranking never leaves students without lists. Results carry `ranking`. |
| `app/routers/search_index.py` | `POST /runs` takes `kind` (`vectors` / `similar_lists` / `full`) and `use_reranker`; asking for the reranker without a Jina key returns **400** with a clear message. `GET /status?use_reranker=` returns `vectors_plan` and `lists_plan` (for the chosen ranking), plus `live_use_reranker`, `gemini_key_configured`, `jina_key_configured`. `plan` is kept (= `lists_plan`) for older clients. |
| `app/tasks/vector_tasks.py` | **A PDF upload now starts a vectors run only** (embeddings + BM25). Lists are left to an admin's lists run, so an upload never silently rebuilds lists in a different ranking from the one being tested. Until then, the new paper's questions are searched live. |
| `app/scripts/backfill_question_embeddings.py` | Now builds **vectors only**. `--skip-similar` is still accepted (always true now); `--subject` moved to the new script. |
| `app/scripts/build_similar_lists.py` (**new**) | Builds the lists from existing vectors: `--reranker` (Jina) or default (RRF, free), `--subject` for trials, `--dry-run`. |
| `components/admin/views/SearchIndexPanel.tsx`, `services/searchIndexService.ts` | Admin → Similar Questions → Search index is now two steps: **1. Vectors** (pending embeddings, Gemini estimate, "Build vectors") and **2. Similar lists** (ranking choice "Without reranker" / "With Jina reranker", disabled when no Jina key; pending subjects and lists, estimate, warning when vectors are missing, "Build similar lists"). The run table and report show each run's type. |

### 4.3 Tests

`tests/test_search_index.py`: `test_plan_before_first_run` updated (default ranking is RRF, so no Jina calls are
estimated; with the reranker, 10). New: `test_rrf_display_score_scale`, `test_algo_version_depends_on_the_ranking`,
`test_vectors_run_builds_no_lists`, `test_lists_run_skips_embeddings_and_records_the_ranking`,
`test_switching_ranking_rebuilds_but_keeps_serving_the_old_lists`,
`test_reranked_lists_run_without_jina_key_stops_before_touching_lists`, `test_admin_api_kinds_and_reranker_needs_a_key`.

### 4.4 Frontend fixes (2026-11-25, branch `feature/ingestion-pipeline`)

| File | Change | Branch open item |
|---|---|---|
| `components/admin/AdminTopNav.tsx` | `breadcrumbMap` gains `'similar-questions': 'Similar Questions'`; the top bar read "Overview" on that page. | 8.1 #3 |
| `components/admin/QuestionReviewCard.tsx` | Taxonomy line = the parts that exist (subject, topic, subtopic) joined by " › "; no "General Studies". | 8.1 #6 (D9) |
| `components/admin/AdminQuestionPublicationCard.tsx` | `[Subject: ]` left blank when the question has no subject. | 8.1 #6 |
| `components/student/PsychometricReportView.tsx` | Subject groups of the post-exam report use the real subject or a blank name (three places). | 8.1 #6 |
| `pages/ExamEnginePage.tsx` | Question section label and section grouping: blank instead of "General Studies". | 8.1 #6 |
| `components/admin/views/SearchIndexPanel.tsx` | While a run is active, polls only that run (`GET /runs/{id}`) every 3 s instead of the full status (which recomputes the whole plan); reloads the full status when the run ends, and every 20th poll (~1 min) because only the status call closes a dead run. | 8.4 #6 |

Not changed on purpose: session titles (guest score summary / dashboard attempt cards in `GuestExamPage.tsx`,
the exam header topic in `ExamEnginePage.tsx`) still fall back to "General Studies", since they name a session,
not a question's subject. The publication card's "General Concepts" / "Core Theme" placeholders for a missing
Microtheme / Nano Theme are also unchanged.

**Impact analysis (GitNexus, before editing):** `SearchIndexPanel`, `AdminTopNav`, `QuestionReviewCard`,
`AdminQuestionPublicationCard`, `PsychometricReportView`: LOW. `ExamEnginePage`: UNKNOWN (no resolved callers);
a text search confirmed its only user is the lazy route in `App.tsx`. `detect-changes` before the commit
reported **critical** (14 symbols, 22 flows), because the changed components sit in large pages (question bank,
review, post-exam report, exam engine); the edits themselves are fallback strings, one map entry and the polling
effect.

**Verification:** `npx tsc --noEmit -p apps/frontend` passes. In the browser (local backend + frontend, admin
account): the breadcrumb reads "Admin › Similar Questions"; with no run active the page makes one status call
and does not poll; a "Rebuild anyway" lists run without the reranker (run #3, nothing to rebuild, under 1 s,
0 lists changed) produced POST `/runs` → `/status` → `/runs/3` → `/status`, then no more requests, and no
console or server errors. Repeated polls during a long run were not observed (no free run with real work was
available: the vectors run for the 158 new 1979 questions calls Gemini).

---

## 5. Database (`Exam_planner_19112026`, local PostgreSQL 18)

| Step | Result |
|---|---|
| Backup | `.db_backups/Exam_planner_19112026_before_similar_questions_2026-11-22.dump` (see §2). |
| First `alembic upgrade head` | **Failed safely, nothing applied** (PostgreSQL rolled it back): `alembic_version` held `072_user_feedback` **three times** and had no primary key, typically the result of restoring a dump into a database that already had the table. |
| Fix | Deleted the two duplicate rows (all three were identical, checked first) and added the standard primary key `alembic_version_pkc (version_num)`, in one transaction. |
| `alembic upgrade head` | 073 → 077 applied. `vector` extension 0.8.7 enabled. **076 reported "Old-pipeline papers: none"**, so no hierarchy links were changed and no backup table was created. |
| Display count (D5) | `site_settings.similar_questions_limit` set from the default 6 to **15**. It stays editable at Admin → Similar Questions (allowed 1–20). |
| New tables | `question_embeddings`, `bm25_term_stats`, `bm25_field_stats`, `question_similarity_lists`, `similarity_subject_state`, `search_index_runs`: all empty until the first vectors run. |

---

## 6. Verification

| Check | Result |
|---|---|
| Similarity test files (`test_similarity_helpers`, `test_subject_filter`, `test_pyq_similar_and_sets`, `test_knowledge_layer`, `test_search_index`) | All pass (search index: 32 of 32, including the 7 new tests). |
| Whole backend suite (`pytest tests`) | **461 passed, 8 failed.** The same 8 fail identically on unmodified `main` (checked in a temporary worktree), so none is caused by this work: `test_admin_notifications_full_lifecycle`, `test_auth::test_refresh_token_flow`, `test_exam_deletion_and_overwrite::test_delete_exam_endpoint`, `test_explanation_translation::test_failed_translation_is_not_remembered`, `test_llm_factory::test_default_llm_settings` (depends on the Gemini model in `.env`), `test_reminders_search_mistakes` ×2, `test_translation_coverage::test_starting_translation`. They match the branch doc's list (SIMILAR_QUESTIONS.md §8.5). |
| GitNexus `detect-changes --scope all` (after re-indexing) | Risk **CRITICAL**: 31 tracked files, 102 symbols, 69 execution flows. Caused by how widely the merge reaches (admin pages, question bank, both practice pages, the settings class, the question-list subject filter). Most relevant flows: `get_similar_questions` and `list_questions` (subject filter now exact, main subject only, see SIMILAR_QUESTIONS.md §10.6). Covered by the test suite above; worth a manual check of practice sessions by subject and the admin Question Bank before release. |
| Frontend type-check (`npx tsc --noEmit -p apps/frontend`) | No errors. |
| Backend start with the merged code | Starts; `/api/v1/admin/search-index/status` is protected (401 without login). |
| Dry runs | Vectors: 2,190 question + 1,990 solution embeddings to make, ~42 Gemini requests (~870k tokens), ~1.5 min. Lists (no reranker): 37 subjects, 3,764 lists, 0 Jina calls, ~1.3 min. |
| `GET /questions/1731/similar` (a Polity question) | 15 results, all Polity (the admin count). Before vectors exist, the hybrid search finds no candidates and the endpoint uses the Knowledge Layer fallback; the log shows the live search embedding the question on the fly (Gemini key works) and 191 same-subject candidates. |
| Guest practice page (`/guest-exam`, Polity) | Loads; the similar-questions panel renders. One question showed 1 suggestion, because before vectors exist the Knowledge Layer fallback finds few matches for some questions. |

### 6.1 First build on the local database (2026-11-22)

| Run | How it was started | Result |
|---|---|---|
| #1 Vectors | Script: `python -m app.scripts.backfill_question_embeddings` | Succeeded in ~2 min: 2,190 question and 1,990 solution embeddings, 0 failures. While it ran, Admin → Similar Questions showed it as "Run #1 in progress · Vectors" (trigger Script) with a progress bar, and locked both start buttons. |
| #2 Similar lists, no reranker | Admin → Similar Questions → "Build similar lists" with "Without reranker" (the same `POST /api/v1/admin/search-index/runs {"kind":"similar_lists","use_reranker":false}` the button sends; the embedded test browser cannot answer the button's OK/Cancel confirmation, so the request was sent from the signed-in admin page) | Succeeded in 40 s: 37 subjects, 3,764 lists, ranking `rrf`, 0 failures. Both steps then showed "Up to date"; stored lists: 3,764 without reranker, 0 reranked. |

After the build:
- `GET /questions/1731/similar` and `/questions/5368/similar`: `source: index` (stored list), 15 results each, all Polity,
  from UPSC CSE, MPSC and CAPF, scores 72–99, all `ranking: rrf`, answered in 23–58 ms. (Before vectors existed, 5368
  got 1 result from the Knowledge Layer fallback.)
- Signed-in practice page, Polity session of 99 UPSC questions: the server returned 15 suggestions (10 MPSC, 3 CAPF,
  2 UPPCS; every UPSC Polity question was in the session and so excluded), answered by live search because too few
  stored items were left after excluding 99. **The panel showed 0**, because its exam toggles start with only UPSC CSE
  on; switching MPSC on showed 10 (top: an MPSC 2022 question on non-constitutional bodies, 97%). See open item 2.
- Admin → Similar Questions shows the count as 15. Its breadcrumb still says "Overview" (branch open item 8.1 #3).

### 6.2 How to run it later

**From the admin page** (Admin → Similar Questions → Search index):
1. **1. Vectors → "Build vectors"** after new questions or edited texts (a PDF upload does this on its own). The card
   shows how many embeddings are pending and the Gemini estimate; with nothing pending the button reads
   "Refresh BM25 statistics".
2. **2. Similar lists**: pick **Without reranker** (free) or **With Jina reranker** (paid; needs `JINA_API_KEY`), check
   the subjects/lists to compute and the estimate, press **Build similar lists** and confirm the OK/Cancel dialog.
   "Rebuild anyway" appears when everything is already up to date for that ranking.
3. While a run is active, a progress card shows its type, phase (Embeddings / BM25 statistics / Similar lists / Done),
   a progress bar and the current subject; the page refreshes itself every 3 seconds. When it ends, "Last run" shows the
   report or why it stopped; "Recent runs" lists the last 10 with type, who started it, duration and counts.
   Only one run at a time; runs started from scripts appear here too.
4. A run started from the admin page runs inside the API process: restarting the backend stops it. Start it again; it
   resumes where it stopped (a stuck run is released after 15 minutes).

**From a terminal** (`cd apps/backend`):
```
uv run python -m app.scripts.backfill_question_embeddings --dry-run   # what would be embedded, estimate
uv run python -m app.scripts.backfill_question_embeddings             # build vectors (prints progress per 100 questions)
uv run python -m app.scripts.build_similar_lists --dry-run            # what lists would be computed, estimate
uv run python -m app.scripts.build_similar_lists                      # lists without the reranker (free)
uv run python -m app.scripts.build_similar_lists --reranker           # lists with the Jina reranker (paid)
uv run python -m app.scripts.build_similar_lists --subject Polity     # one subject only (repeatable), e.g. a trial
```
Each prints its plan first, progress while running, and a summary at the end (`Run #N: SUCCEEDED` plus counts, or the
reason it stopped). The admin page shows the same run live.

**Checking results:** open a question in Admin → Question Bank (Similar Questions Explorer shows each result's score,
the searches that found it and "RRF ranked" / "Jina reranked"), or call
`GET /api/v1/questions/<id>/similar?include_question=true` (`source` = `index` for a stored list, `live` otherwise).

## 7. Open items

| # | Item | Notes |
|---|---|---|
| 1 | ~~Build vectors, then build similar lists.~~ | Done 2026-11-22 (§6.1). |
| 2 | ~~Exam toggles on the practice panels hide cross-exam suggestions~~ (branch open item 8.1 #1). | **Intended (D7), 2026-11-25.** Remaining side effect: the server picks the top 15 across all exams and the browser hides switched-off exams, so the default view can show fewer than 15, or none (§6.1: 15 returned, 0 shown). Filling it would need the chosen exams sent to `/similar`; not decided. |
| 3 | Other branch open items (SIMILAR_QUESTIONS.md §8) | Breadcrumb label and "General Studies" labels fixed (§4.4); admin polling fixed (§4.4). Still open: double request on session load, subject-name differences across exams. |
| 4 | ~~Nothing is committed.~~ | Committed and merged into `main` (§8). |
| 5 | ~~Admin page not checked in a browser.~~ | Checked 2026-11-22 with an admin account (§6.1). |

---

## 8. Git

| When | Step |
|---|---|
| 2026-11-22 | All of the above committed in one commit on branch `feature/similar-questions`. |
| 2026-11-22 | `feature/similar-questions` merged into `main` locally as a fast-forward (`main` had no other commits since `55f0280 first commit`). |
| 2026-11-22 | **Push to GitHub (`origin`, `pranav8799/exam`) refused with HTTP 403**: Git on this machine (Windows Git Credential Manager) is signed in as a different GitHub account that has no write access to the repository. Both branches are to be pushed once the right account is signed in or given access. |
| 2026-11-24 | Fixed by naming the account in this project's remote: `origin` = `https://pranav8799@github.com/pranav8799/exam.git`. Git Credential Manager then asked once for a `pranav8799` sign-in (done in the browser by the owner) and keeps it next to the other account's login, which other projects still use. |
| 2026-11-24 | `feature/similar-questions` and `main` pushed to GitHub (`pranav8799/exam`), together with this log update. |
| 2026-11-25 | Frontend fixes (§4.4) and these doc updates committed on `feature/ingestion-pipeline` and pushed. Not merged into `main`. |

**Included besides the feature files:** `.gitnexusignore` (keeps the branch copy and `.db_backups/` out of the GitNexus
index), `CLAUDE.md` (GitNexus refreshed its index counts), `uv.lock` (adds `pgvector`), this log and
`docs/SIMILAR_QUESTIONS.md`.

**Deliberately left out of the commit** (still on disk, uncommitted):

| Path | Why |
|---|---|
| `package-lock.json` | `npm install` with npm 11 removed some optional peer packages (`lightningcss`, `detect-libc`, …) unrelated to this feature. Not part of this change. |
| `apps/backend/storage_data/…/upsc-prelims-gs1-2095-set_a.pdf` | Created by the test suite (an upload test). Test output, not source. |
| `atharvadevbranchcode/` | The branch copy this work came from; reference only. |
| `.db_backups/` | Local database backup; excluded from git in `.git/info/exclude`. |

**Not stored in git:** the local database changes (migrations 073–077, the `alembic_version` repair, the display count
of 15, the vectors and the 3,764 lists, §5 and §6.1). Any other database (another developer's, staging, production)
needs `alembic upgrade head`, then the vectors and lists runs (§6.2), and its similar-questions count set in the admin
page (it defaults to 6).
