# Ingestion Pipeline — Change Log

Changes made to the ingestion pipeline after the baseline recorded in
[INGESTION_PIPELINE_CURRENT.md](INGESTION_PIPELINE_CURRENT.md) (commit `6edb6f6`).
Branch: `feature/ingestion-pipeline`. Newest change first.

---

## Change 2 — Upload no longer starts AI tagging or embeddings (2026-11-24)

### 2.1 Decision

Ingestion ends at extraction. Nothing that calls an AI model starts automatically after an upload:

- **Knowledge Layer enrichment** (Vertex AI `gemini-2.5-flash`: concepts, entities, relationships, and
  syllabus/difficulty for untagged questions) — **removed from the upload flow**.
- **Embeddings / similar-question index** — **no longer started by an upload**. An admin starts them when
  the papers are ready (reviewed and published), from Admin → Similar Questions or with the two scripts.

Result: **PDF ingestion uses no LLM and no paid API.** Translation (local MarianMT, no API) still starts
after upload as before.

### 2.2 Why

- **Enrichment isn't used by similar questions.** `GET /questions/{id}/similar` serves the stored list,
  else live vector search. Knowledge data was only read by two fallbacks used when both of those fail or
  return nothing; `GET /questions/{id}/knowledge` isn't called by the frontend. Its other job, filling
  Subject/Microtheme/Nano Theme/difficulty for untagged questions, isn't needed when papers carry their tags.
- **Lists can't be built at upload time anyway.** Similar lists only include **published** questions, and
  uploaded questions are `PENDING_HUMAN_REVIEW` until reviewed and the exam is published.
- **The runs already track state in the database**, so one run started later picks up everything:

| Table | Holds | Used to skip finished work |
|---|---|---|
| `search_index_runs` | each run: status, phase, progress, heartbeat, error, `resumed_from_id` | progress on the admin page; a run without heartbeat for 15 min is closed as interrupted |
| `question_embeddings` | each question's vectors + hash of the text they were built from | same hash → not re-embedded (any status, published or not) |
| `similarity_subject_state` | per subject: signature over its published questions | changed signature → subject's lists rebuilt |
| `question_similarity_lists` | each published question's list + the signature/version it was built for | matching signature → skipped |

  So a question published days later changes its subject's signature, and the next lists run rebuilds that
  subject; an edited question gets a new text hash and is re-embedded by the next vectors run.
- **Concurrent upload runs were messy:** only one run can be active, so back-to-back uploads left BM25
  statistics out of date until a later run.

### 2.3 What was changed

| File | Change |
|---|---|
| `apps/backend/app/tasks/exam_tasks.py` (`async_process_exam_pdf`) | Removed the calls to `dispatch_exam_enrichment` and `dispatch_exam_vectorization` after extraction. Translation dispatch and everything before it unchanged. |
| `apps/backend/app/tasks/enrichment_tasks.py` | **Deleted** (only used by that call). The enrichment service itself (`app/knowledge/`) and `python -m app.scripts.backfill_knowledge_layer` stay, for manual use. |
| `apps/backend/app/tasks/vector_tasks.py` | **Deleted** (only used by that call). The runner (`app/services/vector/search_index.py`), the admin page and both scripts are unchanged. |
| `apps/backend/app/tasks/celery_app.py` | Dropped `app.tasks.enrichment_tasks` from the Celery `imports` list. |
| `apps/backend/app/services/vector/search_index.py` | Docstring only: runs are started by an admin or a script, not by uploads. |
| `apps/backend/app/pipeline/regex_question_parser.py` | Docstring only: untagged question banks are no longer tagged by AI automatically. |
| `apps/frontend/src/components/admin/UploadReviewView.tsx` | Mixed question-bank upload note no longer says syllabus/difficulty "are tagged by AI after upload". |
| `docs/SIMILAR_QUESTIONS.md` | "Day to day", file table and known-issues rows updated to say uploads start no run. |

**Impact analysis (GitNexus):** `async_process_exam_pdf` — **HIGH** (4 direct callers: `process_exam_pdf_task`,
`upload_question_paper`, `restart_pipeline_task`, `retry_pipeline_task`; 3 flows). Handled by changing only
the tail of the function (the two background dispatches); run status, return value and error handling are
unchanged, and all four entry points get the same new behaviour on purpose. No tests referenced the
deleted modules; nothing else imported them.

### 2.4 New workflow for a paper

1. Upload the PDF (extraction only, no AI).
2. Review and approve the questions; publish the exam.
3. Admin → Similar Questions → start a **vectors** run (or `python -m app.scripts.backfill_question_embeddings`).
4. Start a **similar lists** run (or `python -m app.scripts.build_similar_lists [--reranker]`).

Steps 3–4 can be done once after many papers. If a run stops, starting it again continues where it stopped.

### 2.5 Consequences to know

| # | Effect | Notes |
|---|---|---|
| 1 | Until step 3 runs, a newly published question shows **no** similar questions (no embedding for live search; knowledge fallbacks now empty for new questions). | Other questions' stored lists also don't include it until step 4. |
| 2 | **Mixed question banks** (`paper_mode=MIXED`, untagged "1. stem [MPSC Mains 2017]" layout) keep the placeholder syllabus (Subject = bank name) and MEDIUM difficulty, as nothing fills them in. | Run `python -m app.scripts.backfill_knowledge_layer` for such a bank if needed (uses Vertex AI). |
| 3 | The `KNOWLEDGE_ENRICHMENT` row in `pipeline_runs` is no longer written for new uploads. | No code reads it (only a pipeline-alert test uses the name as sample data). |
| 4 | Old `search_index_runs` rows with trigger "upload" remain and show as "PDF upload" on the admin page. | History only. |

### 2.6 Tracking checked case by case

The two scripts stay separate: `backfill_question_embeddings` runs a **vectors** run (embeddings + BM25,
never lists) and `build_similar_lists [--reranker]` runs a **similar lists** run (BM25 + lists, never
embeddings). The admin page starts the same two kinds. All use `SearchIndexRunner`, one run at a time.

| Case | What happens | Covered by test |
|---|---|---|
| Nothing changed | Plan says up to date; a run computes nothing and pays nothing | `test_run_stores_lists_then_nothing_is_left_to_do` |
| Run stops on a provider error (no credits, quota, bad key) | Run marked failed with the reason; finished embeddings (saved per batch of 100) and lists (saved one by one) are kept; next run skips them | `test_credit_failure_stops_with_reason_and_next_run_resumes`, `test_embedding_quota_failure_stops_the_run` |
| Script or server killed mid-run | Row stays "running" until its heartbeat is 15 min old (`SEARCH_INDEX_STALE_RUN_MINUTES`), then closed as interrupted; the next run resumes. **A new run can't start during those 15 min.** | `test_one_run_at_a_time_and_stale_runs_are_closed` |
| One question's list fails (e.g. HTTP 400) | Skipped, run ends "warning", subject left out of date, retried next run | `test_single_question_error_is_skipped_and_retried_next_run` |
| Question unpublished at run 1, published later, plus new questions (the "Q4 + Q11–20" case) | Its subject's signature changes → the whole subject is recomputed: Q4 and the new questions get lists and appear in the others' lists; other subjects untouched | `test_question_published_later_and_a_new_paper_are_both_picked_up` (new) |
| Question text, correct answer or solution edited | New text hash → re-embedded by the next vectors run → its subject is recomputed by the next lists run | `test_edited_question_is_reembedded_and_only_its_subject_rebuilt` (new) |
| Question unpublished or moved to another subject | Its stored lists are deleted; the subject's other lists rebuilt | `test_unpublished_question_loses_its_list` |
| Question or exam deleted | Embeddings and lists deleted with it (`ON DELETE CASCADE`); subject signature changes → rebuilt | — |
| Ranking switched (RRF ↔ reranker) or search settings changed | Algorithm version changes → every subject rebuilt; old lists keep being served until replaced | `test_switching_ranking_rebuilds_but_keeps_serving_the_old_lists` |
| Lists run started before the vectors run | Questions without vectors get lists anyway; once vectored, their hash changes the subject signature, so the next lists run rebuilds that subject. The dry run warns "embeddings not built". | — |

Limits to know:
- The unit of tracking for lists is the **subject**: one newly published question recomputes all lists of
  its subject (free without the reranker; with `--reranker` it costs Jina calls for that whole subject).
- Embeddings are made for every question, including unpublished and rejected ones.
- Embeddings are keyed by question id: re-uploading a paper with OVERWRITE creates new question ids, so all
  its questions are embedded again (paid), even if the text is identical.
- Published questions without a subject never get a stored list (they use live search); the plan reports them.

`tests/test_search_index.py`: 34 passed. The full backend suite was not re-run after this change (a run
was started and stopped); the changed and new search-index, parser and pipeline tests pass, the
backend imports cleanly, and only `process_exam_pdf_task` and `sync_exam_s3_task` remain registered
Celery tasks besides translation.

---

## Live test — upload of CSE_Prelims_1979.pdf (2026-11-24/25, both changes)

Local backend (today's code, no Celery/Redis: the job ran inside the API process) and frontend; the PDF
was uploaded from the admin page.

| Check | Result |
|---|---|
| Extraction run (`pipeline_runs`) | `DOCUMENT_EXTRACTION` COMPLETED, ~31 s |
| Questions | 158 stored (exam 104), 0 missing, confidence 1.0 on all, 0 flagged, all `PENDING_HUMAN_REVIEW` |
| `core_concept_raw`, `nature_raw`, `approach_raw` | filled on 158 / 158; value counts identical to the PDF (§1.1) |
| Tag text in question text | none (`[Approach` / `[Nature` / `[Core Concept` in 0 stems) |
| Nano Themes with parentheses | complete (0 with unbalanced brackets) |
| `question_explanations.core_concept` | equals the Core Concept tag on 158 / 158 |
| AI / embeddings after upload | none: no `KNOWLEDGE_ENRICHMENT` run, 0 knowledge links, `knowledge_tags_ai` empty, no `search_index_runs` row |
| Translation | `QUESTION_TRANSLATION` COMPLETED: 1,580 translations, ~24 min |
| Admin review card | Q1 shows "Core concept: Ajanta Caves · Nature: Static – Basic · Approach: Factual" |

Seen during the test, not caused by these changes:
- The paper was uploaded with exam type **UPPCS** (exam 104, `uppcs-prelims-gs1-1979-set_a`, expected size
  150 from that exam type), so the existing **UPSC CSE 1979** paper (exam 80) was not overwritten.
  Re-upload as UPSC CSE with Overwrite and delete exam 104.
- The midnight leaderboard job logs `duplicate key value violates unique constraint
  "leaderboard_snapshots_pkey"`: the table's id sequence is behind its rows. Pre-existing.

---

## Git

| When | Step |
|---|---|
| 2026-11-24 | Baseline doc committed on new branch `feature/ingestion-pipeline` (from `main` at `6edb6f6`). |
| 2026-11-25 | Changes 1 and 2, tests, migration 078 and docs committed on `feature/ingestion-pipeline` and pushed to GitHub (`pranav8799/exam`). Not merged into `main`. |

Left out of the commit on purpose: `package-lock.json` (unrelated npm 11 changes), `apps/backend/storage_data/`
(test output), `atharvadevbranchcode/` (reference copy), `.claude/launch.json` (local dev-server setup,
ignored by git).

Database changes are not in git: migration 078 is applied to the local database only. Any other database
needs `alembic upgrade head` before running this code (§1.6).

---

## Change 1 — Core Concept, Nature and Approach header tags (2026-11-24)

### 1.1 What changed in the question papers

The new paper `CSE_Prelims_1979.pdf` (UPSC CSE Prelims 1979, 165 pages, 158 questions, digital text layer)
has three more tags in every question header:

```
Q1. [Subject: Art & Culture] [Microtheme: Architecture – Cave and Rockcut] [Nano
Theme: Cave Temples & Art Sites (Ajanta, Ellora, Mahabalipuram)]
[Core Concept: Ajanta Caves] [Difficulty: Moderate] [Nature: Static – Basic]
[Approach: Factual]
```

| Tag | In this paper | Values seen |
|---|---|---|
| Subject, Microtheme, Nano Theme, Difficulty | 158 / 158 (as before) | Difficulty: Moderate 156, Easy 2 |
| **Core Concept** (new) | 158 / 158 | free text, e.g. "Ajanta Caves", "Classical Dance", "Sufism" |
| **Nature** (new) | 158 / 158 | Static – Basic 141, Static + Current 6, Current – Event 5, Current – Govt/Report 5, Static – Advanced 1 |
| **Approach** (new) | 158 / 158 | Factual 113, Conceptual 36, Elimination 8, Application 1 |

**Are the new tags used in every exam?** Checked against the other question papers available locally:

| Paper | Questions | Core Concept / Nature / Approach |
|---|---|---|
| CSE_Prelims_1979.pdf (new) | 158 | all three on every question |
| UPSC_CSE_Prelims_1982_GS_Paper1_Full_Solutions-2.pdf | 99 | none (Subject, Microtheme, Nano Theme, Difficulty only) |
| MPSC_Rajyaseva_Prelims_2013_Full_100-1.pdf | 100 | none |
| tests/fixtures/match_list_table_and_diagram.pdf | 3 | none |

So the new tags are consistent **within** the new paper, but older papers don't have them. Whether every
future paper will carry them can't be confirmed from the files. The pipeline therefore treats all three
as **optional**: a paper with them stores them; a paper without them works exactly as before.

**The solution layout also changed in the new paper** (not acted on in this change, see §1.7): sections
"Concept / Background", "Question Analysis", "Must Know / Related Facts" and "Exam Tip:" on all 158
questions, and **no "Common Mistake" or "Reverse Logic Box"** (the 1982 and MPSC 2013 papers have a
Reverse Logic Box on every question).

### 1.2 Problems the current code had with the new paper

Running the baseline extractor and parser on the 1979 paper (before this change):

1. Core Concept, Nature and Approach were **not extracted** at all.
2. **`[Approach: Factual]` leaked into the question text** whenever it sat on its own header line (e.g. Q1, 5,
   12, 13, 16, 18, 19, 21, 24, 25, …): a header line was only recognised if it held a Subject, Topic,
   Subtopic, Difficulty or Type tag. Students would have seen "[Approach: Factual]" as the first line of
   those questions.
3. **Tag values were cut at the first ")"**: `Cave Temples & Art Sites (Ajanta, Ellora, Mahabalipuram)`
   was stored as `… (Ajanta, Ellora, Mahabalipuram` and became the Nano Theme (subtopic) name. This was an
   existing bug, also present in the MPSC 2013 paper (Q20, Q74).

### 1.3 What was changed

| Area | File | Change |
|---|---|---|
| Regex patterns | `apps/backend/app/pipeline/patterns.py` | New `CORE_CONCEPT_TAG_PATTERN` (`Core Concept`, `Core-Concept`), `NATURE_TAG_PATTERN` (`Nature`, but **not** `Question Nature`, which stays an alias of Type as before), `APPROACH_TAG_PATTERN`. All tag patterns are now built by one `_tag_pattern()` helper, so an unbracketed value stops at **any** known tag name, including the new ones. New `HEADER_TAG_PATTERNS` tuple. `extract_header_tags()` now reads a properly bracketed `[Key: value]` up to its matching `]` (parentheses inside the value are kept) and falls back to the old bracket-tolerant patterns for OCR'd headers; it returns three more keys: `core_concept_raw`, `nature_raw`, `approach_raw`. |
| Parser | `apps/backend/app/pipeline/regex_question_parser.py` | `QuestionRecord` gets `core_concept_raw`, `nature_raw`, `approach_raw` (default `None`). A header line holding **any** header tag (incl. the new three) now counts as header, not stem. The three values are filled from `extract_header_tags`. |
| DB model | `apps/backend/app/db/models/question.py` | `Question.core_concept_raw` (VARCHAR 255), `Question.nature_raw` (VARCHAR 100), `Question.approach_raw` (VARCHAR 100), all nullable, next to `subject_raw` … `difficulty_raw`. |
| Migration | `apps/backend/alembic/versions/078_question_concept_nature_tags.py` | Adds the three nullable columns to `questions` (skips any that already exist); downgrade drops them. Revises `077_precomputed_similar_questions`. |
| DB write | `apps/backend/app/pipeline/batch_processor.py` (`commit_question_records_in_batches`) | Stores the three values on insert and on re-extraction. `QuestionExplanation.core_concept` now = **Core Concept tag**, falling back to Nano Theme, then Microtheme (the previous rule) when the paper has no Core Concept. |
| HITL re-extract | `apps/backend/app/hitl/review_service.py` (`trigger_llm_reextraction_for_question`) | Also updates the three values when the re-extracted block has them. |
| Review API | `review_service.get_question_review_detail` | Response gains `core_concept_raw`, `nature_raw`, `approach_raw`. |
| Question API | `apps/backend/app/schemas/question.py` (`QuestionDetailResponse`), `apps/backend/app/services/question_service.py` (both places that build it) | Response gains the same three optional fields (used by the admin review list and question bank). |
| Admin UI | `apps/frontend/src/components/admin/views/QuestionReviewView.tsx`, `apps/frontend/src/components/admin/QuestionReviewCard.tsx` | The review list maps the three fields; the review card shows a read-only line "Core concept: … · Nature: … · Approach: …" under the Subject › Microtheme › Nano Theme line. Hidden for questions that have none. Not editable. |
| Tests | `apps/backend/tests/test_regex_question_parser.py`, `apps/backend/tests/test_deterministic_pipeline.py` | 4 parser tests (new header incl. parentheses and an `[Approach]`-only line; old header unchanged; `Question Nature` still = Type; unbracketed tags stop at the next tag name) and 1 DB test (values stored, NULL for untagged papers, Core Concept used as the explanation's core concept, re-extraction updates in place). |

**Where each tag ends up:**

| Paper tag | Stored in | Shown |
|---|---|---|
| Core Concept | `questions.core_concept_raw` **and** `question_explanations.core_concept` (English) | Admin review card; anywhere the explanation's core concept was already shown (admin Similar Questions explorer, exports, solution JSON, student solution card's correct-option note when the concept isn't already in the solution text) |
| Nature | `questions.nature_raw` | Admin review card |
| Approach | `questions.approach_raw` | Admin review card |

### 1.4 What did *not* change (and why nothing else breaks)

- **Older papers** parse identically. Old vs new parser compared on every question of the 1982 paper (99),
  MPSC 2013 (100) and the test fixture (3): no field differs except the two MPSC 2013 Nano Themes that are
  now complete: Q20 "Landform not produced in a river's upper (youthful) course" and Q74 "Statement about
  ASEAN (cancelled)".
- **`[Type: …]` / `[Question Type: …]` / `[Question Nature: …]`** still go to the question-type tag
  (`knowledge_tags_raw.question_type` / `cognitive_skill`) as before. Approach is kept separate from it:
  Approach describes how to solve the question, while Type describes its format (Chronology,
  Statement-based, …).
- **Syllabus (Subject › Microtheme › Nano Theme), difficulty, options, answer, solution, assets, review
  flags, confidence**: unchanged.
- **Knowledge Layer enrichment, translation, vectorization / similar questions**: unchanged; they don't read
  the new columns. The question text fed to embeddings no longer contains the leaked "[Approach: …]"
  line for new-format papers.
- **Existing questions in the database** keep NULL in the three new columns. There is no backfill: the
  extracted markdown isn't stored, so the values only appear when a paper is uploaded or re-extracted.

### 1.5 Impact analysis (GitNexus, before editing)

| Symbol | Risk | Callers |
|---|---|---|
| `extract_header_tags` | LOW | `parse_question_block` → pipeline, HITL re-extract |
| `parse_question_block` | LOW | `node_regex_parse_and_stitch`, HITL re-extract |
| `QuestionRecord` | LOW | `batch_processor`, `review_service`, `graph` |
| `commit_question_records_in_batches` | LOW | `node_regex_parse_and_stitch` |
| `trigger_llm_reextraction_for_question` | LOW | `routers/review.py: trigger_reextraction` |
| `get_question_review_detail` | LOW | `routers/review.py: get_question_detail` |
| **`Question` (DB model)** | **CRITICAL** (166 dependents, 35 direct) | Used across the whole backend |

The `Question` model risk is handled by keeping the change **additive** (three new nullable columns, nothing
renamed, removed or made required). The one real hazard: every ORM query on `questions` selects the new
columns, so **migration 078 must be applied before the new code runs** against a database (see §1.6).

### 1.6 Deploying / other databases

1. `cd apps/backend && uv run alembic upgrade head` — **before** starting the new backend code.
2. Restart the API and Celery workers.
3. Re-upload (OVERWRITE) or re-extract any new-format paper ingested before this change to fill the tags
   and remove the leaked `[Approach: …]` line from its question text.

Local database (`localhost`, development): migration 078 applied on 2026-11-24; downgrade → upgrade
tested; 2,190 existing questions unaffected (new columns NULL).

### 1.7 Not done — open points

| # | Item | Notes |
|---|---|---|
| 1 | Show Nature / Approach to students | Product decision. Currently admin-only. |
| 2 | Filter or analyse by Nature / Approach | e.g. Static vs Current practice sets, Approach in reports. Values are free text; "Nature" combines two parts (Static/Current + Basic/Advanced/Event/Govt-Report) that could be split if filters are wanted. |
| 3 | Edit the three tags in the review editor | Read-only today. |
| 4 | New solution layout | "Concept / Background", "Question Analysis", "Must Know / Related Facts", "Exam Tip:" are kept as plain paragraphs of the solution text. No Reverse Logic Box → wrong-option analysis is taken from "Option (B) Gujarat is wrong: …" sentences in Question Analysis; for the **last** option it runs on into the "Must Know / Related Facts" section. The correct option's analysis is the whole solution. "Exam Tip" isn't stored separately. |
| 5 | Feed Core Concept to the Knowledge Layer AI tagger | Would change the prompt version and re-run AI tagging for all questions; not done. |
| 6 | Truncated Nano Themes already in the database | Questions uploaded before this change keep subtopic names cut at ")" (e.g. MPSC 2013 Q20, Q74). A re-upload creates the corrected subtopic; the old truncated subtopic row stays in that exam's syllabus. |

### 1.8 Verification

- New parser on the 1979 paper: 158 questions, 0 extraction issues, all 7 tags on 158/158, no tag text left
  in any question text, no Nano Theme with unbalanced parentheses.
- Old vs new parser on the 1982, MPSC 2013 and fixture papers: identical except the two corrected Nano
  Themes (§1.4).
- `tests/test_regex_question_parser.py` + `tests/test_deterministic_pipeline.py`: 24 passed.
- Full backend suite: 466 passed, 8 failed. The same 8 also fail on the code **before** this change
  (checked by re-running them with the change stashed), so they are pre-existing and unrelated:
  `test_admin_notifications_full_lifecycle`, `test_refresh_token_flow`, `test_delete_exam_endpoint`,
  `test_failed_translation_is_not_remembered`, `test_default_llm_settings`,
  `test_question_search_and_search_practice`, `test_mistake_notebook`, `test_starting_translation`.
- Frontend `tsc --noEmit`: passes.
- Migration 078: upgrade, downgrade, upgrade on the local DB; ORM read of `questions` works.
