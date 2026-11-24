# Ingestion Pipeline — Current Implementation (baseline)

**Recorded:** 2026-11-24, at commit `6edb6f6` on `main`.
**Purpose:** a record of how question-paper ingestion works *today*, written before any changes, so later
changes can be compared against it. It describes the code as it is, including rough edges; §11 lists
observations without proposing fixes.
**Changes made since this baseline** are logged in [INGESTION_PIPELINE_CHANGES.md](INGESTION_PIPELINE_CHANGES.md).

---

## 1. One-paragraph summary

An admin uploads a PDF from the admin panel. The backend saves it to storage, creates an `Exam` row and
starts a background job (Celery when a worker is online, otherwise an in-process asyncio task). The job
runs a 4-step LangGraph workflow: **(1)** turn the PDF into one markdown document (pypdf/pdfplumber for PDFs
with a text layer, Docling + RapidOCR for scanned/image PDFs), **(2)** split the markdown into question
blocks with regexes and parse each block into a `QuestionRecord` (stem, options, answer, solution, common
mistake, reverse-logic box, tags, assets), then write them to the database in batches of 10, **(3)** set
every question to `PENDING_HUMAN_REVIEW`, **(4)** log completion. Afterwards the run is marked `COMPLETED` or
`WAITING_FOR_HITL`, and three follow-up jobs are fired: translation, Knowledge Layer enrichment (LLM
tagging) and vectorization (embeddings + BM25 for similar questions). Nothing becomes visible to students
until a reviewer approves each question and then publishes the whole exam.

There is **no LLM in the extraction itself** — it is deterministic (OCR + regex). LLMs are used only
afterwards, in enrichment.

---

## 2. End-to-end flow

```
Admin UI (UploadReviewView → useExamUpload → examService.uploadExamPaper)
        │  POST /api/v1/exams/upload   (multipart: file, paper_mode, exam_name, year, set_code,
        │                               exam_type(_id), conflict_resolution, marks/negative marks)
        ▼
ExamService.handle_pdf_upload                      app/services/exam_service.py:52
  ├─ parse filename → canonical_code, set, stage, subject, year   (or mixed_bank_tokens for MIXED)
  ├─ resolve ExamType (id → type → exam_name → "upsc")
  ├─ marking scheme (form values, else exam type's, else 2 / 0.67) + validation
  ├─ duplicate check → 409 (CHECK) | return SKIPPED (SKIP) | delete old exam(s) (OVERWRITE)
  ├─ save temp file → storage.upload_file → uploads/question_papers/{code}/{set}/{code}.pdf
  ├─ seed local PDF cache, delete temp file
  ├─ INSERT Exam (pdf_path, total_questions, marks, is_mixed_bank, …)
  └─ dispatch: Celery process_exam_pdf_task.delay(…)   or   BackgroundTasks(async_process_exam_pdf)
        ▼
async_process_exam_pdf                              app/tasks/exam_tasks.py:45
  ├─ derive title from filename if needed
  ├─ ensure_local_pdf (download from storage into ./temp_pdf_cache if not local) + page count
  ├─ update Exam.pdf_path / total_pages (or create Exam if missing)
  ├─ SyllabusService.sync_or_create_exam_syllabus(exam_id)  (warning only on failure)
  ├─ PipelineRun DOCUMENT_EXTRACTION → RUNNING, step EXTRACTING_DOCUMENT_WITH_DOCLING_OCR, 30%
  ├─ LangGraph pipeline.ainvoke(state)               app/pipeline/graph.py
  │     ocr_convert → regex_parse_and_stitch → mcq_validator_and_hitl → export_artifacts
  ├─ PipelineManagementService.on_pipeline_1_completed  → COMPLETED | WAITING_FOR_HITL
  ├─ if ENABLE_TRANSLATION_PIPELINE: translate_exam_questions_task (Celery) / asyncio task
  ├─ dispatch_exam_enrichment   → KNOWLEDGE_ENRICHMENT run (LLM concepts/entities/syllabus/difficulty)
  ├─ dispatch_exam_vectorization → search-index "vectors" run (embeddings + BM25)
  └─ finally: clear PDF cache + delete ./temp_pdf_cache/<file>
        ▼
Human review (admin.review)  → each question approved → status PUBLISHED
        ▼
POST /admin/exams/{id}/approve → every question must be PUBLISHED → Exam.status PUBLISHED, is_active True
```

---

## 3. Entry points (how a run starts)

| Endpoint | File | Used by | Notes |
|---|---|---|---|
| `POST /exams/upload` | `routers/exams.py:52` → `ExamService.handle_pdf_upload` | **Admin upload UI** (`frontend/src/services/examService.ts:68`) | Main path. Supports `paper_mode` (`EXAM_WISE` / `MIXED`), marking scheme, `run_mode` (`auto`/`celery`/`background`), `conflict_resolution` (`CHECK`/`OVERWRITE`/`SKIP`). |
| `POST /pipeline/upload-question-paper` | `routers/pipeline.py:150` | Not called by the current frontend | Older path. `year` required, `conflict_resolution` `CHECK`/`IGNORE`/`OVERWRITE`/`RESTART`, stores PDF at `uploads/question_papers/{code}/{code}.pdf` (no set folder), no marking scheme, no mixed mode, creates Exam with `is_active=False`, registers task under `qp_{exam_id}`. |
| `POST /pipeline/restart/{exam_id}` | `routers/pipeline.py:596` | `adminService.ts:198` | Cancels task key `{exam_id}`, sets run RUNNING 0%, re-runs the whole job on the stored `exam.pdf_path`. |
| `POST /pipeline/retry/{exam_id}` | `routers/pipeline.py:648` | `adminService.ts:210` | Same as restart, different step label/task id prefix. |
| `POST /admin/exams/{exam_id}/resume-pipeline` | `routers/admin.py:918` | `adminService.ts:295` | Re-runs extraction only if the exam has **0** questions and a PDF; otherwise answers "already completed". Uses pipeline type `QUESTION_EXTRACTION` (old name). |
| `POST /pipeline/stop/{exam_id}` | `routers/pipeline.py:552` | `adminService.ts:186` | Cancels task keys `qp_`, `ak_`, `exp_` and bare id; marks RUNNING runs STOPPED. |
| HITL "re-extract" one question | `hitl/review_service.py:469` | review screen | Re-runs the extractor on the **whole PDF** and rewrites one question's stem/options/answer (see §8). |

**Which executor runs the job** (`ExamService`, `run_mode="auto"`): Celery if `CELERY_TASK_ALWAYS_EAGER` is
on or `has_active_celery_workers()` finds a worker (ping, cached 5 s); else FastAPI `BackgroundTasks` in the
API process. If the Celery dispatch throws, it falls back to `BackgroundTasks`. Celery task time limit is
3600 s (`tasks/celery_app.py`).

### 3.1 Filename parsing and exam identity
- `EXAM_WISE`: `config/filename_parser.parse_question_paper_filename` builds `canonical_code`, `set_code`,
  `stage`, `subject_code`, `year` from the filename plus form fields. Duplicate detection uses
  `ExamRepository.get_all_matching_canonical_exams(code, year, stage, subject_code, set, exam_name)`.
- `MIXED` (question bank): `mixed_bank_tokens(filename)` — no exam/year/set asked. Duplicate = same code
  only. `total_questions` is left `None` (the bank's size is whatever the PDF holds). Only an exam named in
  the filename (e.g. `MPSC_Test4.pdf`) sets marks/timing; there is no UPSC default.
- `OVERWRITE` calls `ExamService.delete_exam` for every match — the old exam and all its data are deleted
  before the new one is created (new `exam_id`).

---

## 4. Pipeline state and graph

`app/pipeline/graph.py` — a strictly linear LangGraph `StateGraph(OverallPipelineState)`:

```
START → ocr_convert → regex_parse_and_stitch → mcq_validator_and_hitl → export_artifacts → END
```

`OverallPipelineState` (`app/pipeline/state.py:141`) carries `pdf_path`, `exam_id`, `exam_title`,
`output_dir`, `total_pages`, `task_id`, `full_markdown`, `docling_pictures`, `docling_tables`,
`docling_page_images`, `detected_format`, `question_records`, `processed_question_keys`, plus fields
left over from the old vision pipeline (`execution_pass`, `retry_count`, `target_retry_pages`).
`ainvoke` is passed `max_concurrency=MAX_VISION_CONCURRENCY` (default 5), which has no effect on a
linear graph.

---

## 5. Step 1 — `ocr_convert` (PDF → markdown + assets)

File: `app/pipeline/docling_ocr_extractor.py`, class `DoclingOcrExtractor.extract()`.
Runs in a thread-pool executor so the event loop stays free. If `full_markdown` is already in the
state (tests), the step is skipped.

### 5.1 Choosing the route
pypdf reads every page's text. If **≥ 50 %** of pages have text, the PDF is treated as **digital**;
otherwise as **rasterized** (scanned / "Print to PDF" images).

### 5.2 Digital route (no OCR) — `extract_digital_layout`
- pdfplumber extracts positioned text lines (with characters) for every page.
- **Running headers/footers** are detected (`detect_page_furniture`): first/last lines (and the line next
  to them) repeated on ≥ 50 % of pages, ignoring a trailing page number, and never text that also appears
  mid-page. They and bare page numbers are removed.
- **Tables**: `page.find_tables()`; only real grids (≥ 2 rows and ≥ 2 columns) count — 1-column boxes
  (callouts, question headers) are ignored. Converted to GFM markdown (`rows_to_markdown`). A table that
  starts a page with the same column count as the table that ended the previous page is treated as a
  continuation: its rows are appended to the earlier table (no new header/marker).
- **Pictures**: embedded images with both sides ≥ 40 pt.
- Pages **without** tables/pictures keep pypdf's content-stream text (keeps author reading order, incl.
  multi-column). Pages **with** them are rebuilt top-to-bottom from pdfplumber lines, inserting
  `<!-- TABLE id=N -->` + table markdown and `<!-- PICTURE id=N -->` at their vertical position.
- Bboxes are scaled ×1.5 (`DIGITAL_RENDER_SCALE`) to match page images rendered with pypdfium2 at
  scale 1.5 (fallback: pdf2image at 108 dpi).
- Undecoded bullets (`\x7f`, `(cid:127)`) are turned into `•`.
- If layout extraction throws, it falls back to plain pypdf text with no tables/pictures.

### 5.3 Rasterized route (OCR) — Docling
- `DocumentConverter` with `do_ocr=True`, `RapidOcrOptions`, table structure on (`TableFormerMode.FAST`),
  `generate_page_images=True`. The converter is created lazily per extractor instance.
- `_docling_markdown_and_assets` walks `doc.iterate_items()` in reading order: pictures → marker,
  tables → marker + table markdown, everything else → its markdown/text. Bboxes are converted from PDF
  points (bottom-left origin) to top-left page-image pixels.

### 5.4 Common output
- Every page is preceded by `<!-- PAGE_BREAK p=N -->`.
- Two normalisations push `Q<N>.` headers onto their own line (also re-applied in the splitter).
- Returns `{full_markdown, pictures[{id,page_no,bbox,self_ref}], tables[{id,page_no,bbox,markdown}],
  page_images{page_no: PIL.Image}}`. Page images are held in memory for the whole run.

---

## 6. Step 2 — `regex_parse_and_stitch` (markdown → questions → DB)

### 6.1 Format detection — `detect_format`
`EXPLAINED_QA` if the markdown has at least one `Answer` line **and** one `Solution` header; otherwise
`RAW_EXAM`. `RAW_EXAM` only logs a warning — parsing is the same either way (questions will simply lack
answers/solutions and get flagged).

### 6.2 Splitting into blocks — `split_into_question_blocks`
Two layouts are supported (`app/pipeline/patterns.py` holds all regexes):

1. **Tagged papers** — lines matching `Q_HEADER_PATTERN` (`Q1.`, `Q 1`, `Q.1`, `# Q1`, …). Used whenever
   at least one such header exists.
2. **Numbered question banks** — only if there are **no** `Q<N>` headers: lines `N. stem…`
   (`NUMBERED_Q_HEADER_PATTERN`). Because numbered lines also occur inside questions, only the
   **single longest run of consecutive numbers** (N, N+1, …; at least 2) is accepted as question starts.

For each block: page numbers it spans (from `PAGE_BREAK` markers), its picture/table ids (from markers),
clean text with page-break/asset markers and chapter headings (`— Economic Planning —`) removed, and
`section` = the last chapter heading before it. Numbered-bank blocks are rewritten to start with `Q<N>.`
so both layouts look the same to the parser.

### 6.3 Parsing one block — `parse_question_block` → `QuestionRecord`
Order of operations (`app/pipeline/regex_question_parser.py:305`):

1. **Knowledge tags removed first** (`knowledge_tags.extract_knowledge_tags`): `[Concepts: …]`,
   `[Entities: …]`, `[Relationships: A -> B; A -(rel)-> B]`, also bare `Concepts:` lines before the first
   option. Kept on the record, never in stem/solution.
2. **Section boundaries**: first option line, `Answer` line, `Solution` header, `Common Mistake:` header,
   `Reverse Logic Box` header. The options start at the **last** `A` option before the answer (a stem may
   itself contain `(A) … (B) …` statements).
3. **Header tags** from the header line and following tag lines: `Subject`, `Topic`/`Microtheme`,
   `Sub-topic`/`Nano Theme`, `Difficulty`, `Type`/`Question Type`. Tolerant of `[ ( { <` brackets and
   tags wrapped across lines. Difficulty mapped: easy/simple/basic → EASY; tough/hard/difficult/advanced →
   HARD; anything else → MEDIUM.
4. **Stem**: lines between header and options. Question-bank `➤` lines become their own paragraphs;
   otherwise lines are joined with blank lines around markdown tables and before a closing prompt
   ("Select…", "Which…", "Codes:") after a numbered list. A trailing source tag `[MPSC Mains 2017]` is
   cut out of the stem into `source_raw` / `source_year`. Numbered/roman statement lines are also
   collected into `statement_lines`.
5. **Options A–D** (`(A)`, `[A]`, `A)`, `A.`), multi-line options joined.
6. **Answer**: `Answer: (B) text`, `Ans B`, `Answer B` → `answer_label`, `answer_text`.
7. **Solution**: bullets and paragraphs; wrapped bullet lines rejoined; "Note:" paragraphs kept apart; a
   `Related Articles / Acts / Cases:` list is captured into `related_refs` (and stays in the text).
   `solution_text` = paragraphs joined with blank lines.
8. **Common mistake** text.
9. **Reverse Logic Box**: long form `Option (A): Why it seems tempting — … What's actually true — …`, or
   short form `(A) 3 2 4 1 — <why wrong>. Trap: <why tempting>`.
10. **Assets**: pictures/tables whose ids were inside the block. (Older extractions without ids fall back
    to page + vertical-position matching, `disambiguate_asset_question`.)

**Confidence score** (starts at 1.0, clamped 0–1) and issues:

| Issue | Penalty |
|---|---|
| `EMPTY_STEM` | −0.35 |
| `MISSING_OPTION_A/B/C/D` (each) | −0.20 |
| `NO_ANSWER_LINE` | −0.40 |
| `ANSWER_LABEL_NOT_IN_OPTIONS` | −0.25 |
| `EMPTY_SOLUTION` | −0.20 |
| `MISSING_VISUAL_ASSET` (stem says "following figure/table…" but no picture/table/inline table) | −0.25 |

Knowledge-tag problems (`UNPARSED_RELATIONSHIP`) do **not** affect confidence.

### 6.4 Writing to the database — `BatchQuestionRegistrar.commit_question_records_in_batches`
File `app/pipeline/batch_processor.py:526`. Called with `batch_size=10` (hard-coded in `graph.py`; the
`PIPELINE_BATCH_SIZE` setting, default 5, is not used here). An in-process `asyncio.Lock` per exam
serialises concurrent commits for the same exam.

**Before the batches**
- Load exam (code, name, `paper_set_code`; `source_year` is ignored for mixed banks).
- Get or create the active `SyllabusVersion` (`{code}_V1`), preload its Subjects/Topics/Subtopics.
- Marking scheme from `scoring.exam_marking_scheme(exam, exam_type)` (exam's own → type's → UPSC).

**Per batch (one transaction)** — matching key is `(exam_id, question_number_in_paper)`:
1. Bulk-load existing questions and their English translation, options, option translations,
   reasoning, explanation, primary hierarchy link and review task.
2. Create missing **Subject → Topic → Subtopic** rows from the raw tags. Fallbacks when tags are absent:
   Subject = exam name, Topic = subject, Subtopic = topic. Codes are the first 10 chars upper-cased.
3. **Question** insert or update: `subtopic_id`, `difficulty`, `source_year` (`[… 2017]` tag wins over
   the exam's year), `is_complete` (= confidence ≥ 0.8 and no missing option), `has_image`,
   `page_numbers_json`, raw tags, `extraction_confidence`, `extraction_issues_json`,
   `knowledge_tags_raw`, `requires_human_review` (= confidence < 0.8 or any issue). New questions get
   `question_type="SINGLE_CHOICE"`, `expected_time_seconds=60`, `section_code="DEFAULT"`,
   `status=PENDING_HUMAN_REVIEW`.
4. **QuestionHierarchyLink** (primary, weight 1.0) to the subtopic.
5. **QuestionTranslation** (ENGLISH) = stem.
6. **QuestionOption** per label, `is_correct = label == answer_label`, display order A=1…D=4.
7. **OptionTranslation** (ENGLISH) with `distractor_analysis`: for the correct option the solution text
   (cut before any reverse-logic part); for wrong options "Why it seems tempting — … / What's actually
   true — …" from the reverse-logic box, else a per-option extract from the solution text.
8. **QuestionOptionReasoning** (`why_tempting`, `whats_actually_true`) where the box has that option.
9. **QuestionExplanation** (ENGLISH): `detailed_solution`, `common_mistake`, `common_traps_json`,
   `core_concept` (= subtopic or topic tag).
10. **QuestionAsset**: all existing assets for the question are **deleted**, then pictures are cropped
    from the page image and saved to `cropped_assets/{code}_{set}_q{N}_p{page}_{rand}.png` via the storage
    provider (row kept with `asset_path=None` if cropping fails); tables are stored as `table_markdown`
    (asset type `DIAGRAM` for both).
11. **QuestionMetadata** baseline if missing: Bloom's `ANALYZE`, IRT a=1.5, b=0, c=0.25,
    `cognitive_skill` = `Type` tag or "Conceptual Reasoning".
12. **HumanReviewTask** (`PENDING_REVIEW`, flagged by validator, reasons = issues) when confidence < 0.8 or
    any issue.
13. Commit, then invalidate the question/exam caches for the batch.

A failing batch is rolled back, counted in `failed_batches`, and the run **continues**.

**After the batches** — exam counters: `extracted_questions_count`, `missing_questions_count`,
`missing_question_numbers_json`. Normal paper: missing = numbers 1…`total_questions` not found. Mixed bank:
`total_questions` set to the span min…max and only gaps count.

If any batch failed, `notify_pipeline_failure(..., partial=True)` alerts admins, because the run will
still end as completed.

---

## 7. Steps 3–4 and run status

- **`mcq_validator_and_hitl`**: one bulk `UPDATE questions SET status=PENDING_HUMAN_REVIEW` for the exam's
  questions that are neither `PENDING_HUMAN_REVIEW` nor `PUBLISHED`.
- **`export_artifacts`**: log line only — no files are written (the `output_artifacts/<stem>` directory is
  created by the task but left empty).
- **`on_pipeline_1_completed`** (`services/pipeline_management_service.py:78`): if any question has
  `is_complete = False`, a `HumanReviewTask` with reason `INCOMPLETE_QUESTION_SEAM_CUT` is added where none
  exists, and the `DOCUMENT_EXTRACTION` run becomes `WAITING_FOR_HITL` (100 %); otherwise `COMPLETED`
  (100 %).

**`pipeline_runs` row** (one per exam per `pipeline_type`, updated in place):

| Moment | status | current_step | % |
|---|---|---|---|
| `/pipeline/upload-question-paper` accepted | RUNNING | `PDF_UPLOADED_STARTING_QUESTION_EXTRACTION` | 5 |
| Before graph | RUNNING | `EXTRACTING_DOCUMENT_WITH_DOCLING_OCR` | 30 |
| Success | COMPLETED / WAITING_FOR_HITL | `DOCUMENT_EXTRACTION_COMPLETE` / `FLAGGED_QUESTIONS_REQUIRE_ADMIN_REVIEW` | 100 |
| Exception | FAILED | `FAILED_WITH_ERROR` (+ `error_log`) | 0 |
| Cancelled | STOPPED | `CANCELLED_BY_ADMIN` | 0 |

`FAILED` triggers `pipeline_alert_service.notify_pipeline_failure`. Every status update invalidates the
pipeline-status cache. `GET /pipeline/status/{exam}` returns the overview (question extraction, answer
key, explanation indexing, translation — the latter three are names from the older 3-pipeline design).

---

## 8. After extraction

### 8.1 Background follow-ups (fired from `async_process_exam_pdf`, never block extraction)
| Job | Code | What it does | Run type |
|---|---|---|---|
| Translation | `tasks/translation_tasks.py` → `QuestionTranslationService.translate_exam` | Only if `ENABLE_TRANSLATION_PIPELINE` (default **True**, engine `open_source` MarianMT or `google`). Target = `ACTIVE_TRANSLATION_LANGUAGE_CODES`. A midnight reconciliation task backfills gaps. | `QUESTION_TRANSLATION` |
| Knowledge enrichment | `tasks/enrichment_tasks.py` → `knowledge/enrichment_service.py` | LLM (`KNOWLEDGE_AI_MODEL_NAME`, default `gemini-2.5-flash`, 8 questions per call) extracts concepts/entities/relationships into `knowledge_tags_ai`, links them to canonical knowledge nodes, and — only for questions still on the placeholder syllabus rows — fills Subject › Microtheme › Nano Theme and difficulty. Rule-based extraction is the fallback. Cached per question text + prompt version. | `KNOWLEDGE_ENRICHMENT` |
| Vectorization | `tasks/vector_tasks.py` → `SearchIndexRunner().run("upload", kind="vectors")` | Gemini embeddings + BM25 statistics for the paper. Similar-question **lists** are not rebuilt here; an admin runs that. If another index run is active, only this exam's embeddings are made. | search-index run (Admin → Similar Questions) |

Each is sent to Celery if a worker is online, else started with `asyncio.create_task` in the current
process.

### 8.2 Human review (HITL) — `app/hitl/review_service.py`
- **All** extracted questions start as `PENDING_HUMAN_REVIEW`; `HumanReviewTask` rows exist only for
  flagged ones.
- `update_and_approve_question`: reviewer may edit passage, stem, options, correct answer → question
  `PUBLISHED`, `is_complete=True`, integrity re-evaluated, task `VERIFIED`, reviewer points awarded.
- `reject_question`: question `REJECTED`, task `REJECTED`.
- `trigger_llm_reextraction_for_question`: despite the name, re-runs the deterministic extractor on the
  **whole PDF** (`exam.pdf_path`, passed directly), finds the block with the same number and overwrites
  stem, options, correct answer, raw tags, difficulty. Solution/explanation and assets are not updated.

### 8.3 Exam publish — `POST /admin/exams/{id}/approve` (`routers/admin.py:1141`)
Refuses unless **every** question is `PUBLISHED`; refuses (and logs a `S3_PUBLISH` run `CORRUPTED`) if any
question has no text or no options. Then sets `Exam.status=PUBLISHED`, `is_active=True` and records an
`S3_PUBLISH` run `COMPLETED`. No JSON files are written to S3 any more (the docstring still says so).

---

## 9. Data written (tables touched by one ingestion)

`exams`, `pipeline_runs`, `syllabus_versions`, `subjects`, `topics`, `subtopics`, `questions`,
`question_hierarchy_links`, `question_translations`, `question_options`, `option_translations`,
`question_option_reasoning`, `question_explanations`, `question_assets` (+ image files in storage),
`question_metadata`, `human_review_tasks`. Follow-up jobs add translations, knowledge links
(`question_concepts`, `question_entities`, `question_relationships`, …) and `question_embeddings` / BM25
tables.

Storage paths: original PDF `uploads/question_papers/{code}/{set}/{code}.pdf` (main path);
cropped pictures `cropped_assets/…png`. Local scratch: `./temp_uploads`, `./temp_pdf_cache`.

---

## 10. Settings that affect ingestion

| Setting | Default | Effect |
|---|---|---|
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | `redis://localhost:6379/0` | Celery queue |
| `CELERY_TASK_ALWAYS_EAGER` | False (True in tests) | Run Celery tasks inline |
| `STORAGE_TYPE`, `S3_*` | — | Where PDFs and cropped images go |
| `ENABLE_TRANSLATION_PIPELINE` | True | Post-extraction translation |
| `TRANSLATION_ENGINE` | `open_source` | MarianMT or Google |
| `KNOWLEDGE_AI_ENABLED`, `KNOWLEDGE_AI_MODEL_NAME`, `KNOWLEDGE_AI_BATCH_SIZE`, `KNOWLEDGE_LLM_*` | True, gemini-2.5-flash, 8 | Enrichment LLM |
| `PIPELINE_BATCH_SIZE` | 5 | Not used by the current path (graph passes 10) |
| `MAX_VISION_CONCURRENCY` | 5 | Passed to LangGraph; no effect on the linear graph |
| `OUTPUT_ARTIFACTS_DIR` | `<root>/output_artifacts` | Directory created per run, nothing written |

Tests covering this area: `tests/test_deterministic_pipeline.py`, `test_regex_question_parser.py`,
`test_table_and_diagram_extraction.py`, `test_batch_and_db_fallback.py`, `test_pipeline.py`,
`test_pipeline_alerts.py`, `test_admin_recent_uploads.py`, `test_exam_deletion_and_overwrite.py`.

---

## 11. Observations about the current behaviour (no changes made)

Facts noticed while reading the code, recorded so they are not mistaken for intended behaviour later.

1. **Two upload endpoints** with different rules (§3): the UI uses `/exams/upload`; `/pipeline/upload-question-paper`
   is older, stores the PDF at a different path and has no marking scheme or mixed mode.
2. **Re-extraction never deletes**: questions are matched by number and updated. A question number that
   disappears from a new extraction, or an option label that disappears, keeps its old row.
3. **Step 3 resets `REJECTED` questions** to `PENDING_HUMAN_REVIEW` on every run (the bulk update only
   excludes PENDING and PUBLISHED).
4. **Question banks: only one numbering run is kept.** Without `Q<N>` headers, only the longest
   consecutive 1, 2, 3… run is used, so a bank whose numbering restarts per chapter keeps only one chapter.
5. **Everything is `SINGLE_CHOICE`** with `expected_time_seconds=60`; `calculate_question_expected_time`
   is only used by the old path.
6. **Failed DB batches still end `COMPLETED`/`WAITING_FOR_HITL`** (with a partial-failure alert).
7. **Progress is coarse**: 5 % → 30 % → 100 %, nothing during OCR or the commit loop.
8. **Leftover code from the old vision-LLM pipeline**: `build_question_registry`,
   `commit_questions_in_batches`, `dsu_stitcher.py`, `PageExtractionPayload` and related schemas in
   `state.py`, the old state fields, and `pipeline_db_fields_mapping.md`/`README.md` text describing
   25-question commits. `dsu_stitcher.async_crop_and_save_asset` is still imported by `review_service.py`.
9. **Task tracking differs by path**: `/exams/upload` with Celery does not call `register_celery_task`,
   so `/pipeline/stop` may not be able to revoke that Celery task; the API routes register under
   `qp_{id}` or `{id}` inconsistently.
10. **Celery `imports`** lists exam, sync, translation and enrichment task modules but not
    `vector_tasks`; the vectorization task is registered only once something imports that module in the
    worker.
11. **HITL re-extract** re-OCRs the whole PDF for one question and reads `exam.pdf_path` directly
    (the main run goes through `ensure_local_pdf`, which downloads from storage first).
12. **Page images for the whole PDF are kept in memory** until the commit step finishes.
13. **Tables and pictures share asset type `DIAGRAM`**; a table is told apart only by `table_markdown`.
