"""Search index: plan/estimate, tracked runs, incremental subjects, resume, failures, stored-list reads, admin API.

SQLite in memory; Gemini, Jina and the pgvector search are replaced by fakes, so nothing is paid for.
"""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.cache import get_cache
from app.config.settings import settings
from app.core.security import create_access_token, get_password_hash
from app.db.models.enums import LanguageCode, QuestionStatus
from app.db.models.hierarchy import Exam, Subject, Subtopic, SyllabusVersion, Topic
from app.db.models.question import Question, QuestionTranslation
from app.db.models.search_index import QuestionSimilarityList, SearchIndexRun, SimilaritySubjectState
from app.db.models.user import User, UserRole
from app.services.vector import search_index as si
from app.services.vector.provider_errors import ProviderError, ProviderFailure, classify, missing_key
from app.services.vector.search_index import RunAlreadyActive, SearchIndexRunner, build_plan, subject_snapshot
from app.services.vector.stored_similarities import read_stored


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False},
                                 poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture(autouse=True)
def no_pending_embeddings(monkeypatch):
    """Embedding candidates come from the real query in one test only; elsewhere nothing is pending."""
    async def none(**kwargs):
        return []
    monkeypatch.setattr("app.scripts.backfill_question_embeddings.fetch_question_candidates", none)


async def _add(session, obj):
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


async def seed(factory):
    """Polity: 3 published (one via the syllabus hierarchy) + 1 draft; History: 2 published; 1 without subject."""
    async with factory() as s:
        e19 = await _add(s, Exam(code="U19", name="UPSC 2019", source_year=2019, is_active=True))
        e22 = await _add(s, Exam(code="U22", name="UPSC 2022", source_year=2022, is_active=True))
        ver = await _add(s, SyllabusVersion(exam_id=e22.id, version_code="v1", effective_from=datetime(2020, 1, 1)))
        subj = await _add(s, Subject(version_id=ver.id, name="Polity"))
        topic = await _add(s, Topic(subject_id=subj.id, name="Constitution"))
        sub = await _add(s, Subtopic(topic_id=topic.id, name="Preamble"))

        def q(exam, n, subject=None, status=QuestionStatus.PUBLISHED, **kw):
            return Question(exam_id=exam.id, question_number_in_paper=n, status=status, subject_raw=subject, **kw)

        ids = {
            "p1": (await _add(s, q(e22, 1, "Polity"))).id,
            "p2": (await _add(s, q(e19, 1, "polity "))).id,                 # same subject after normalising
            "p3": (await _add(s, q(e22, 2, "Wrong tag", subtopic_id=sub.id))).id,  # hierarchy wins
            "p_draft": (await _add(s, q(e22, 3, "Polity", status=QuestionStatus.DRAFT))).id,
            "h1": (await _add(s, q(e22, 4, "History"))).id,
            "h2": (await _add(s, q(e19, 2, "History"))).id,
            "none": (await _add(s, q(e22, 5, None))).id,
        }
        for qid in ids.values():
            s.add(QuestionTranslation(question_id=qid, language=LanguageCode.ENGLISH, question_text=f"Question text number {qid}"))
        await s.commit()
        ids["e19"], ids["e22"] = e19.id, e22.id
        return ids


class FakeService:
    """Stands in for HybridSimilarityService: other published questions of the same subject, by id."""
    calls = []
    fail = None  # callable(qid, mode) -> Optional[ProviderError]

    def __init__(self, session):
        self.session = session

    async def find_similar_questions(self, qid, limit=None, include_question=False, other_years_only=False):
        mode = "other_years" if other_years_only else "default"
        FakeService.calls.append((qid, mode))
        if FakeService.fail is not None:
            err = FakeService.fail(qid, mode)
            if err is not None:
                raise err
        subjects = await subject_snapshot(self.session)
        pool = next((i.published_ids for i in subjects.values() if qid in i.published_ids), [])
        others = [o for o in pool if o != qid]
        return [{"question_id": o, "similarity_score": 95 - n, "match_reasons": ["Matched via test"], "raw_score": 0.5}
                for n, o in enumerate(others)][:limit]


@pytest.fixture(autouse=True)
def reset_fake():
    FakeService.calls, FakeService.fail = [], None
    yield


def runner(factory, **kw):
    return SearchIndexRunner(session_factory=factory, service_factory=FakeService, list_concurrency=1, **kw)


async def rows(factory, model):
    async with factory() as s:
        return (await s.exec(select(model))).all()


# ── provider errors ─────────────────────────────────────────────────────────

def test_provider_errors_are_classified_with_actionable_messages():
    assert classify(ProviderFailure("jina", 402, "")).code == "jina_no_credits"
    assert classify(ProviderFailure("gemini", 429, "")).code == "gemini_quota"
    assert classify(ProviderFailure("gemini", 403, "")).code == "gemini_auth"
    assert classify(ProviderFailure("jina", 503, "")).code == "jina_server"
    assert classify(ProviderFailure("jina", None, "timeout")).code == "jina_unreachable"
    assert classify(ProviderFailure("jina", 400, "too long")).code == "jina_http_400"
    assert "GEMINI_API_KEY" in classify(ProviderFailure("gemini", 401, "")).message
    assert "resumes" in classify(ProviderFailure("jina", 402, "")).message
    assert missing_key("jina").code == "jina_no_key"


@pytest.mark.anyio
async def test_jina_strict_mode_raises_and_default_mode_keeps_input_order(monkeypatch):
    from app.services.vector.jina_reranker import JinaReranker

    async def post(self, url, **kw):
        return httpx.Response(402, text="no balance", request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", post)

    with pytest.raises(ProviderError) as err:
        await JinaReranker(api_key="k", raise_on_error=True).rerank("q", ["a", "b"])
    assert err.value.code == "jina_no_credits"
    lenient = await JinaReranker(api_key="k").rerank("q", ["a", "b"])
    assert [r["index"] for r in lenient] == [0, 1]


# ── plan and subjects ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_subject_snapshot_uses_hierarchy_first_and_published_only(factory):
    ids = await seed(factory)
    async with factory() as s:
        subjects = await subject_snapshot(s)
    assert set(subjects) == {"polity", "history"}
    assert subjects["polity"].published_ids == sorted([ids["p1"], ids["p2"], ids["p3"]])
    assert ids["p_draft"] not in subjects["polity"].published_ids


@pytest.mark.anyio
async def test_plan_before_first_run(factory):
    await seed(factory)
    plan = await build_plan(factory)
    assert plan["totals"]["subjects"] == 2
    assert plan["totals"]["published_with_subject"] == 5
    assert plan["totals"]["published_without_subject"] == 1
    assert plan["pending"]["subjects_out_of_date"] == 2
    assert plan["pending"]["lists_to_compute"] == 5 * 2  # two modes per published question
    # Default ranking is the fused one (no reranker): no Jina calls are estimated
    assert plan["ranking"] == "rrf"
    assert plan["estimate"]["jina"]["rerank_calls"] == 0
    assert "credits" in plan["estimate"]["note"]
    assert plan["up_to_date"] is False
    reranked = await build_plan(factory, use_reranker=True)
    assert reranked["ranking"] == "rerank"
    assert reranked["estimate"]["jina"]["rerank_calls"] == 10


def test_algo_version_changes_with_search_settings(monkeypatch):
    before = si.algo_version()
    monkeypatch.setattr(settings, "SIMILARITY_RRF_K", settings.SIMILARITY_RRF_K + 1)
    assert si.algo_version() != before


# ── runs ────────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_run_stores_lists_then_nothing_is_left_to_do(factory):
    ids = await seed(factory)
    run = await runner(factory).run("cli")
    assert run.status == "succeeded", run.error_message
    assert run.phase == "done"
    assert run.summary["lists_computed"] == 10 and run.summary["subjects_updated"] == 2
    lists = await rows(factory, QuestionSimilarityList)
    assert len(lists) == 10
    p1 = next(r for r in lists if r.question_id == ids["p1"] and r.mode == "default")
    assert [i["id"] for i in p1.items] == [ids["p2"], ids["p3"]]  # same subject only, never the draft
    assert p1.complete is True
    assert len(await rows(factory, SimilaritySubjectState)) == 2

    plan = await build_plan(factory)
    assert plan["up_to_date"] is True and plan["pending"]["lists_to_compute"] == 0
    again = await runner(factory).run("cli")
    assert again.summary["lists_computed"] == 0  # nothing paid for twice


@pytest.mark.anyio
async def test_new_question_recomputes_only_its_subject(factory):
    ids = await seed(factory)
    await runner(factory).run("cli")
    async with factory() as s:
        s.add(Question(exam_id=ids["e19"], question_number_in_paper=9, status=QuestionStatus.PUBLISHED,
                       subject_raw="Polity"))
        await s.commit()
    plan = await build_plan(factory)
    assert [x["subject"] for x in plan["pending"]["out_of_date_subjects"]] == ["Polity"]
    FakeService.calls = []
    run = await runner(factory).run("upload")
    assert run.summary["lists_computed"] == 4 * 2
    assert {qid for qid, _ in FakeService.calls}.isdisjoint({ids["h1"], ids["h2"]})


@pytest.mark.anyio
async def test_unpublished_question_loses_its_list(factory):
    ids = await seed(factory)
    await runner(factory).run("cli")
    async with factory() as s:
        q = await s.get(Question, ids["p2"])
        q.status = QuestionStatus.DRAFT
        s.add(q)
        await s.commit()
    run = await runner(factory).run("cli")
    assert run.summary["lists_removed"] == 1  # one question, both modes
    assert all(r.question_id != ids["p2"] for r in await rows(factory, QuestionSimilarityList))


@pytest.mark.anyio
async def test_credit_failure_stops_with_reason_and_next_run_resumes(factory):
    await seed(factory)
    calls = {"n": 0}

    def no_credits_after_three(qid, mode):
        calls["n"] += 1
        return classify(ProviderFailure("jina", 402, "")) if calls["n"] > 3 else None

    FakeService.fail = no_credits_after_three
    failed = await runner(factory).run("admin")
    assert failed.status == "failed"
    assert failed.error_code == "jina_no_credits"
    assert "credits" in failed.error_message
    assert failed.phase == "similar_lists"  # where it stopped
    stored_before = len(await rows(factory, QuestionSimilarityList))
    assert stored_before == 3

    FakeService.fail, FakeService.calls = None, []
    resumed = await runner(factory).run("admin")
    assert resumed.status == "succeeded"
    assert resumed.resumed_from_id == failed.id
    assert resumed.summary["lists_reused"] == 3
    assert resumed.summary["lists_computed"] == 10 - 3
    assert len(FakeService.calls) == 7  # finished lists were not recomputed (not paid again)


@pytest.mark.anyio
async def test_single_question_error_is_skipped_and_retried_next_run(factory):
    ids = await seed(factory)
    FakeService.fail = lambda qid, mode: (classify(ProviderFailure("jina", 400, "bad doc"))
                                          if qid == ids["h1"] and mode == "default" else None)
    run = await runner(factory).run("cli")
    assert run.status == "warning"
    assert run.summary["lists_failed"] == 1
    assert run.progress["similar_lists"]["failed_examples"][0]["question_id"] == ids["h1"]
    assert "live search" in run.summary["warning"]
    plan = await build_plan(factory)
    assert [x["subject"] for x in plan["pending"]["out_of_date_subjects"]] == ["History"]

    FakeService.fail = None
    retry = await runner(factory).run("cli")
    assert retry.status == "succeeded" and retry.summary["lists_computed"] == 1


@pytest.mark.anyio
async def test_embedding_quota_failure_stops_the_run(factory, monkeypatch):
    await seed(factory)

    async def candidates(**kw):
        return [{"question_id": 1, "is_uptodate": False, "published": True, "embed_question": True,
                 "question_text": "x", "embed_explanation": False, "explanation_text": None}]

    class Embedder:
        api_key, last_failure = "k", None

    embedder = Embedder()

    async def process_batch(batch, emb, concurrency=8, stats=None):
        emb.last_failure = ProviderFailure("gemini", 429, "quota")
        stats["question_failed"] = len(batch)
        return 0

    monkeypatch.setattr("app.scripts.backfill_question_embeddings.fetch_question_candidates", candidates)
    monkeypatch.setattr("app.scripts.backfill_question_embeddings.process_batch", process_batch)
    run = await runner(factory, embedder=embedder).run("cli")
    assert run.status == "failed" and run.error_code == "gemini_quota" and run.phase == "embeddings"
    assert await rows(factory, QuestionSimilarityList) == []


@pytest.mark.anyio
async def test_one_run_at_a_time_and_stale_runs_are_closed(factory):
    await seed(factory)
    r = runner(factory)
    first = await r.create_run("admin")
    with pytest.raises(RunAlreadyActive):
        await r.create_run("cli")
    async with factory() as s:
        row = await s.get(SearchIndexRun, first.id)
        row.status, row.heartbeat_at = "running", datetime.now(timezone.utc) - timedelta(hours=1)
        s.add(row)
        await s.commit()
    second = await r.create_run("cli")  # the dead run no longer blocks
    async with factory() as s:
        dead = await s.get(SearchIndexRun, first.id)
    assert dead.status == "failed" and dead.error_code == "interrupted"
    assert second.resumed_from_id == first.id


# ── stored-list reads ───────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_read_stored_rechecks_items_and_falls_back(factory, monkeypatch):
    ids = await seed(factory)
    await runner(factory).run("cli")
    async with factory() as s:
        got = await read_stored(s, ids["p1"], "default", limit=5)
        assert [g["question_id"] for g in got] == [ids["p2"], ids["p3"]]  # complete list: fewer is fine
        got = await read_stored(s, ids["p1"], "default", limit=5, excluded={ids["p2"]})
        assert [g["question_id"] for g in got] == [ids["p3"]]  # excluded ids never shown
        assert await read_stored(s, ids["none"], "default", limit=5) is None  # nothing stored → live
        assert await read_stored(s, ids["p1"], "default", limit=5, version="other") is None  # old algorithm

        # An item unpublished after the run is dropped at read time
        q = await s.get(Question, ids["p3"])
        q.status = QuestionStatus.DRAFT
        s.add(q)
        await s.commit()
        got = await read_stored(s, ids["p1"], "default", limit=5)
        assert [g["question_id"] for g in got] == [ids["p2"]]

        # An incomplete list that runs short after filtering → live search fills the slots
        row = await s.get(QuestionSimilarityList, (ids["p1"], "default"))
        row.complete = False
        s.add(row)
        await s.commit()
        assert await read_stored(s, ids["p1"], "default", limit=2) is None
        assert len(await read_stored(s, ids["p1"], "default", limit=1)) == 1

        # The question moved to another subject → its stored list no longer applies
        q = await s.get(Question, ids["p1"])
        q.subject_raw = "Economy"
        s.add(q)
        await s.commit()
        assert await read_stored(s, ids["p1"], "default", limit=1) is None


# ── HTTP: student endpoint and admin API ───────────────────────────────────

@pytest.fixture
async def client(factory, monkeypatch):
    from app.db.session import get_async_session
    from app.fastapi_server import app

    async def override():
        async with factory() as s:
            yield s

    app.dependency_overrides[get_async_session] = override
    monkeypatch.setattr("app.routers.search_index._session_factory", lambda: factory)
    await get_cache().delete_pattern("*")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_async_session, None)
    await get_cache().delete_pattern("*")


async def _users(factory):
    async with factory() as s:
        admin = await _add(s, User(full_name="Super", email="super@index.test", role=UserRole.SUPER_ADMIN,
                                   password_hash=get_password_hash("x")))
        student = await _add(s, User(full_name="Stu", email="stu@index.test", role=UserRole.STUDENT,
                                     password_hash=get_password_hash("x")))
    return ({"Authorization": f"Bearer {create_access_token(subject=admin.id, role='SUPER_ADMIN')}"},
            {"Authorization": f"Bearer {create_access_token(subject=student.id, role='STUDENT')}"})


@pytest.mark.anyio
async def test_similar_endpoint_serves_stored_list(client, factory):
    ids = await seed(factory)
    await runner(factory).run("cli")
    body = (await client.get(f"/api/v1/questions/{ids['p1']}/similar",
                             params={"limit": 2, "exclude_ids": str(ids["p3"])})).json()
    assert body["source"] == "index"
    assert [r["question_id"] for r in body["results"]] == [ids["p2"]]
    assert body["results"][0]["tier"] == "exact"
    other = (await client.get(f"/api/v1/questions/{ids['p1']}/similar",
                              params={"limit": 2, "other_years_only": "true"})).json()
    assert [r["question_id"] for r in other["results"]] == [ids["p2"]]  # 2019 paper only
    assert other["results"][0]["source_year"] == 2019


@pytest.mark.anyio
async def test_similar_endpoint_can_be_switched_to_live(client, factory, monkeypatch):
    ids = await seed(factory)
    await runner(factory).run("cli")
    monkeypatch.setattr(settings, "SIMILARITY_USE_PRECOMPUTED", False)
    body = (await client.get(f"/api/v1/questions/{ids['p1']}/similar", params={"limit": 2})).json()
    assert body.get("source") != "index"


@pytest.mark.anyio
async def test_admin_api_status_start_conflict_and_permissions(client, factory):
    await seed(factory)
    admin, student = await _users(factory)
    assert (await client.get("/api/v1/admin/search-index/status", headers=student)).status_code == 403
    assert (await client.post("/api/v1/admin/search-index/runs", json={}, headers=student)).status_code == 403

    status = (await client.get("/api/v1/admin/search-index/status", headers=admin)).json()
    assert status["plan"]["pending"]["lists_to_compute"] == 10
    assert status["active_run"] is None

    async with factory() as s:  # a run in progress blocks a second one
        await _add(s, SearchIndexRun(status="running", trigger="cli", heartbeat_at=datetime.now(timezone.utc)))
    busy = await client.post("/api/v1/admin/search-index/runs", json={}, headers=admin)
    assert busy.status_code == 409
    status = (await client.get("/api/v1/admin/search-index/status", headers=admin)).json()
    assert status["active_run"]["status"] == "running"
    runs = (await client.get("/api/v1/admin/search-index/runs", headers=admin)).json()
    assert len(runs) == 1
    assert (await client.get(f"/api/v1/admin/search-index/runs/{runs[0]['id']}", headers=admin)).json()["trigger"] == "cli"
    assert (await client.get("/api/v1/admin/search-index/runs/999", headers=admin)).status_code == 404


@pytest.mark.anyio
async def test_admin_api_start_records_who_started_it(client, factory, monkeypatch):
    await seed(factory)
    admin, _ = await _users(factory)
    executed = []

    async def fake_execute(self, run_id, on_progress=None):
        executed.append(run_id)

    monkeypatch.setattr(SearchIndexRunner, "execute", fake_execute)
    resp = await client.post("/api/v1/admin/search-index/runs", json={"skip_similar": True}, headers=admin)
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued" and body["trigger"] == "admin" and body["triggered_by_id"] is not None
    assert body["options"]["skip_similar"] is True
    assert body["estimate"]["pending"]["lists_to_compute"] == 10
    import asyncio
    await asyncio.sleep(0)
    assert executed == [body["id"]]


@pytest.mark.anyio
async def test_subject_option_limits_the_run(factory):
    ids = await seed(factory)
    run = await runner(factory).run("cli", options={"subjects": ["History"]})
    assert run.summary["lists_computed"] == 2 * 2
    assert {qid for qid, _ in FakeService.calls} == {ids["h1"], ids["h2"]}
    plan = await build_plan(factory)
    assert [x["subject"] for x in plan["pending"]["out_of_date_subjects"]] == ["Polity"]


# ── separate vectors / similar-lists runs, optional reranker (2026-11-22) ───

def test_rrf_display_score_scale():
    from app.services.vector.similarity_retrieval_service import rrf_display_score
    k = 60
    best = 1.0 / (k + 1)                              # 1st in every channel (weights sum to 1.0)
    assert rrf_display_score(best, best) == 99
    assert rrf_display_score(0.30 / (k + 1), best) == 58          # 1st in one 0.30-weight channel only
    assert rrf_display_score(1.0 / (k + 10), best) == 91          # 10th everywhere
    assert rrf_display_score(1.0 / (k + 50), best) == 73          # 50th everywhere
    assert rrf_display_score(0.0, best) == 40 and rrf_display_score(0.1, 0.0) == 40


def test_algo_version_depends_on_the_ranking(monkeypatch):
    rrf, rerank = si.algo_version(False), si.algo_version(True)
    assert rrf != rerank
    assert si.current_versions() == {rrf: "rrf", rerank: "rerank"}
    # Reranker settings only matter for reranked lists
    monkeypatch.setattr(settings, "SIMILARITY_RERANK_TOP_N", settings.SIMILARITY_RERANK_TOP_N + 1)
    assert si.algo_version(False) == rrf and si.algo_version(True) != rerank


@pytest.mark.anyio
async def test_vectors_run_builds_no_lists(factory):
    await seed(factory)
    run = await runner(factory).run("cli", options={"kind": "vectors"})
    assert run.status == "succeeded", run.error_message
    assert run.options["kind"] == "vectors"
    assert "similar_lists" not in (run.progress or {})
    assert FakeService.calls == [] and await rows(factory, QuestionSimilarityList) == []
    plan = await build_plan(factory, kind="vectors")
    assert plan["pending"]["lists_to_compute"] == 0 and plan["estimate"]["jina"]["rerank_calls"] == 0


@pytest.mark.anyio
async def test_lists_run_skips_embeddings_and_records_the_ranking(factory, monkeypatch):
    ids = await seed(factory)

    class NoEmbedder:
        api_key = "k"

        async def embed_texts_batch(self, *a, **kw):
            raise AssertionError("a similar-lists run must not embed anything")

    run = await runner(factory, embedder=NoEmbedder()).run("cli", options={"kind": "similar_lists"})
    assert run.status == "succeeded", run.error_message
    assert "embeddings" not in (run.progress or {})
    assert run.summary["lists_computed"] == 10 and run.summary["ranking"] == "rrf"
    lists = await rows(factory, QuestionSimilarityList)
    assert {r.algo_version for r in lists} == {si.algo_version(False)}
    assert all(i["ranking"] == "rrf" for r in lists for i in r.items)
    async with factory() as s:
        got = await read_stored(s, ids["p1"], "default", limit=5)
    assert got and all(m["ranking"] == "rrf" for m in got)


@pytest.mark.anyio
async def test_switching_ranking_rebuilds_but_keeps_serving_the_old_lists(factory):
    ids = await seed(factory)
    await runner(factory).run("cli", options={"kind": "similar_lists"})
    assert (await build_plan(factory, kind="similar_lists"))["up_to_date"] is True
    # The other ranking sees every subject as out of date …
    reranked_plan = await build_plan(factory, kind="similar_lists", use_reranker=True)
    assert reranked_plan["pending"]["subjects_out_of_date"] == 2
    assert reranked_plan["totals"]["stored_lists_by_ranking"] == {"rrf": 10, "rerank": 0, "outdated": 0}
    # … while students keep getting the lists built without the reranker
    async with factory() as s:
        assert await read_stored(s, ids["p1"], "default", limit=5)
    FakeService.calls = []
    run = await runner(factory).run("cli", options={"kind": "similar_lists", "use_reranker": True})
    assert run.status == "succeeded" and run.summary["lists_computed"] == 10 and run.summary["ranking"] == "rerank"
    assert {r.algo_version for r in await rows(factory, QuestionSimilarityList)} == {si.algo_version(True)}
    async with factory() as s:
        got = await read_stored(s, ids["p1"], "default", limit=5)
    assert got and all(m["ranking"] == "rerank" for m in got)


@pytest.mark.anyio
async def test_reranked_lists_run_without_jina_key_stops_before_touching_lists(factory, monkeypatch):
    await seed(factory)
    await runner(factory).run("cli", options={"kind": "similar_lists"})
    before = {(r.question_id, r.mode, r.algo_version) for r in await rows(factory, QuestionSimilarityList)}
    monkeypatch.setattr(settings, "JINA_API_KEY", None)
    real = SearchIndexRunner(session_factory=factory, list_concurrency=1)  # real service: needs the key
    run = await real.run("cli", options={"kind": "similar_lists", "use_reranker": True})
    assert run.status == "failed" and run.error_code == "jina_no_key"
    assert {(r.question_id, r.mode, r.algo_version) for r in await rows(factory, QuestionSimilarityList)} == before


@pytest.mark.anyio
async def test_admin_api_kinds_and_reranker_needs_a_key(client, factory, monkeypatch):
    await seed(factory)
    admin, _ = await _users(factory)
    executed = []

    async def fake_execute(self, run_id, on_progress=None):
        executed.append(run_id)

    monkeypatch.setattr(SearchIndexRunner, "execute", fake_execute)
    status = (await client.get("/api/v1/admin/search-index/status?use_reranker=true", headers=admin)).json()
    assert status["lists_plan"]["ranking"] == "rerank" and status["vectors_plan"]["kind"] == "vectors"
    assert status["lists_plan"]["estimate"]["jina"]["rerank_calls"] == 10

    monkeypatch.setattr(settings, "JINA_API_KEY", None)
    no_key = await client.post("/api/v1/admin/search-index/runs",
                               json={"kind": "similar_lists", "use_reranker": True}, headers=admin)
    assert no_key.status_code == 400 and "JINA_API_KEY" in no_key.json()["detail"]

    ok = await client.post("/api/v1/admin/search-index/runs", json={"kind": "similar_lists"}, headers=admin)
    assert ok.status_code == 202
    assert ok.json()["options"]["kind"] == "similar_lists" and ok.json()["options"]["use_reranker"] is False
    assert (await client.post("/api/v1/admin/search-index/runs", json={"kind": "nope"}, headers=admin)).status_code == 422


# ── pacing against the reranker's rate limit ────────────────────────────────

class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


@pytest.mark.anyio
async def test_rate_limiter_paces_tokens_per_minute():
    from app.services.vector.rate_limiter import TokenRateLimiter
    clock = FakeClock()
    lim = TokenRateLimiter(1000, clock=clock, sleep=clock.sleep)
    await lim.acquire(600)
    await lim.acquire(300)
    assert clock.slept == []            # 900 of 1000 used
    await lim.acquire(300)              # does not fit: waits for the window to clear
    assert 59 < sum(clock.slept) < 61
    clock.slept.clear()
    await lim.acquire(5000)             # bigger than the budget, but alone in a new window... after it clears
    assert sum(clock.slept) > 59


@pytest.mark.anyio
async def test_rate_limiter_backs_off_on_429_and_uses_real_usage():
    from app.services.vector.rate_limiter import TokenRateLimiter
    clock = FakeClock()
    lim = TokenRateLimiter(1000, min_tokens_per_minute=500, clock=clock, sleep=clock.sleep)
    await lim.acquire(400)
    lim.record(400, 100)                # Jina reported fewer tokens: more room left
    await lim.acquire(850)
    assert clock.slept == []
    wait = lim.rate_limited()
    assert wait == 60 and lim.budget == 750 and lim.rate_limit_hits == 1
    await lim.acquire(10)               # everyone waits until the window has cleared
    assert 59 < sum(clock.slept) <= 60.1
    for _ in range(5):
        lim.rate_limited()
    assert lim.budget == 500            # never below the floor


@pytest.mark.anyio
async def test_strict_reranker_pauses_on_429_then_succeeds(monkeypatch):
    from app.services.vector.jina_reranker import JinaReranker
    from app.services.vector.rate_limiter import TokenRateLimiter
    clock = FakeClock()
    responses = [429, 200]

    async def post(self, url, **kw):
        code = responses.pop(0)
        body = {"results": [{"index": 0, "relevance_score": 0.9}], "usage": {"total_tokens": 50}}
        return httpx.Response(code, json=body if code == 200 else {"detail": "rate"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    rr = JinaReranker(api_key="k", raise_on_error=True)
    rr.limiter = TokenRateLimiter(100_000, clock=clock, sleep=clock.sleep)
    out = await rr.rerank("q", ["a"])
    assert out[0]["index"] == 0
    assert rr.limiter.rate_limit_hits == 1 and rr.limiter.budget == 75_000
    assert 59 < sum(clock.slept) <= 60.1  # paused for the window instead of hammering


@pytest.mark.anyio
async def test_run_keeps_one_jina_request_in_flight_and_retries_concurrency_429(monkeypatch):
    import asyncio
    from app.services.vector.jina_reranker import JinaReranker
    from app.services.vector.rate_limiter import TokenRateLimiter
    state = {"in_flight": 0, "max": 0, "calls": 0}

    async def post(self, url, **kw):
        state["calls"] += 1
        state["in_flight"] += 1
        state["max"] = max(state["max"], state["in_flight"])
        await asyncio.sleep(0.01)
        state["in_flight"] -= 1
        if state["calls"] == 1:  # someone else (a page view) holds the key's other slot
            return httpx.Response(429, json={"detail": "Concurrency limit exceeded: 2/2 concurrent requests.",
                                             "code": "RATE_CONCURRENCY_LIMIT_EXCEEDED"},
                                  request=httpx.Request("POST", url))
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 1.0}], "usage": {"total_tokens": 5}},
                              request=httpx.Request("POST", url))

    async def no_wait(_):
        return None

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    monkeypatch.setattr("app.services.vector.jina_reranker.asyncio.sleep", no_wait)
    limiter = TokenRateLimiter(1_000_000, max_concurrent=1)
    rerankers = [JinaReranker(api_key="k", raise_on_error=True) for _ in range(4)]
    for r in rerankers:
        r.limiter = limiter
    results = await asyncio.gather(*[r.rerank("q", ["a"]) for r in rerankers])
    assert all(res for res in results)
    assert state["max"] == 1                    # never more than one request in flight
    assert limiter.rate_limit_hits == 0         # a concurrency 429 does not slow the token pace
    assert limiter.budget == 1_000_000


def test_concurrency_429_has_its_own_reason():
    err = classify(ProviderFailure("jina", 429, '{"detail":"Concurrency limit exceeded: 2/2 concurrent requests"}'))
    assert err.code == "jina_concurrency" and "same time" in err.message


@pytest.mark.anyio
async def test_queued_requests_respect_a_pause_that_started_while_they_waited(monkeypatch):
    import asyncio
    from app.services.vector.jina_reranker import JinaReranker
    from app.services.vector.rate_limiter import TokenRateLimiter
    clock = FakeClock()
    codes = [429, 200, 200, 200]
    hits = []

    async def post(self, url, **kw):
        code = codes.pop(0)
        hits.append((clock.now, code))
        await asyncio.sleep(0)
        body = {"results": [{"index": 0, "relevance_score": 1.0}], "usage": {"total_tokens": 5}}
        return httpx.Response(code, json=body if code == 200 else {"detail": "tokens per minute"},
                              request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    limiter = TokenRateLimiter(1_000_000, max_concurrent=1, clock=clock, sleep=clock.sleep)
    rerankers = [JinaReranker(api_key="k", raise_on_error=True) for _ in range(3)]
    for r in rerankers:
        r.limiter = limiter
    await asyncio.gather(*[r.rerank("q", ["a"]) for r in rerankers])
    assert limiter.rate_limit_hits == 1           # one 429, not one per queued request
    assert all(t >= hits[0][0] + 59 for t, _ in hits[1:])  # everything after it waited for the pause


def test_limiter_learns_the_key_limit_from_jinas_message():
    from app.services.vector.rate_limiter import TokenRateLimiter, key_limit_from_message
    msg = '{"detail":"Token rate limit exceeded: 101,801/100,000 tokens per minute. Reduce batch sizes"}'
    assert key_limit_from_message(msg) == 100_000
    assert key_limit_from_message("Concurrency limit exceeded: 2/2 concurrent requests") is None
    lim = TokenRateLimiter(1_000_000)
    lim.rate_limited(msg)
    assert lim.budget == 80_000          # 80% of the key's limit, the rest for page views
    lim.rate_limited(msg)
    assert lim.budget == 80_000          # stays there; it does not keep shrinking


# ── tracking across publish / new papers / edits (the runs are the only trigger) ──

from app.scripts import backfill_question_embeddings as _backfill  # noqa: E402

_REAL_FETCH_CANDIDATES = _backfill.fetch_question_candidates  # saved before the autouse fixture patches it


@pytest.mark.anyio
async def test_question_published_later_and_a_new_paper_are_both_picked_up(factory):
    """Q4 is a draft during the first run; later it is published and new questions arrive. The next run
    lists Q4 and the new questions, and rebuilds the lists in their subject (they may now include them)."""
    ids = await seed(factory)
    first = await runner(factory).run("cli", options={"kind": "similar_lists"})
    assert first.summary["lists_computed"] == 10
    assert all(r.question_id != ids["p_draft"] for r in await rows(factory, QuestionSimilarityList))

    async with factory() as s:
        draft = await s.get(Question, ids["p_draft"])
        draft.status = QuestionStatus.PUBLISHED
        s.add(draft)
        new_ids = []
        for n in (11, 12):
            q = await _add(s, Question(exam_id=ids["e19"], question_number_in_paper=n,
                                       status=QuestionStatus.PUBLISHED, subject_raw="Polity"))
            new_ids.append(q.id)
        await s.commit()

    plan = await build_plan(factory, kind="similar_lists")
    assert [x["subject"] for x in plan["pending"]["out_of_date_subjects"]] == ["Polity"]

    FakeService.calls = []
    second = await runner(factory).run("cli", options={"kind": "similar_lists"})
    assert second.status == "succeeded", second.error_message
    # Polity now has 6 published questions, each list recomputed in both modes; History untouched
    assert second.summary["lists_computed"] == 6 * 2
    assert {qid for qid, _ in FakeService.calls}.isdisjoint({ids["h1"], ids["h2"]})
    lists = {(r.question_id, r.mode): r for r in await rows(factory, QuestionSimilarityList)}
    assert (ids["p_draft"], "default") in lists and all((q, "default") in lists for q in new_ids)
    p1_items = [i["id"] for i in lists[(ids["p1"], "default")].items]
    assert ids["p_draft"] in p1_items and set(new_ids) <= set(p1_items)
    assert (await build_plan(factory, kind="similar_lists"))["up_to_date"] is True


@pytest.mark.anyio
async def test_edited_question_is_reembedded_and_only_its_subject_rebuilt(factory, monkeypatch):
    """Editing a question's text marks it for re-embedding; once its vector is rebuilt, its subject's
    lists are out of date and only that subject is recomputed."""
    from sqlalchemy import text as sql
    from app.services.vector.gemini_embedder import compute_sha256

    ids = await seed(factory)
    async with factory() as s:
        for qid in [v for k, v in ids.items() if not k.startswith("e")]:
            await s.exec(sql(
                "INSERT INTO question_embeddings (question_id, question_dense_embedding, question_hash, "
                "embedding_model, created_at, updated_at) VALUES (:q, '[0.1]', :h, 'm', :t, :t)"
            ).bindparams(q=qid, h=compute_sha256(f"Question text number {qid}"), t=datetime.now(timezone.utc)))
        await s.commit()

    async def fetch(**kw):
        return await _REAL_FETCH_CANDIDATES(session_factory=factory, **{k: v for k, v in kw.items() if k != "session_factory"})

    assert all(c["is_uptodate"] for c in await fetch())
    await runner(factory).run("cli", options={"kind": "similar_lists"})

    async with factory() as s:
        tr = (await s.exec(select(QuestionTranslation).where(QuestionTranslation.question_id == ids["p1"]))).first()
        tr.question_text = "Edited question text"
        s.add(tr)
        await s.commit()
    pending = [c for c in await fetch() if not c["is_uptodate"]]
    assert [(c["question_id"], c["embed_question"], c["embed_explanation"]) for c in pending] == [(ids["p1"], True, False)]

    # What the vectors run stores for it: the new vector and the new text hash
    async with factory() as s:
        await s.exec(sql("UPDATE question_embeddings SET question_hash = :h WHERE question_id = :q")
                     .bindparams(h=compute_sha256("Edited question text"), q=ids["p1"]))
        await s.commit()
    assert all(c["is_uptodate"] for c in await fetch())

    FakeService.calls = []
    run = await runner(factory).run("cli", options={"kind": "similar_lists"})
    assert run.summary["lists_computed"] == 3 * 2  # Polity only
    assert {qid for qid, _ in FakeService.calls} == {ids["p1"], ids["p2"], ids["p3"]}
