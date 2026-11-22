"""PYQ: same theme in other years (post-test review) and the paper-set filter for subject-wise tests."""
import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from app.fastapi_server import app
from app.cache import get_cache
from app.db.session import get_async_session
from app.db.models.user import User, UserRole
from app.db.models.hierarchy import Exam
from app.db.models.question import Question
from app.db.models.enums import QuestionStatus, QuestionSourceType
from app.core.security import create_access_token, get_password_hash

test_engine = create_async_engine("sqlite+aiosqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
_state: dict = {}


async def _override_session():
    async with test_engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        if not _state:
            await _seed(session)
        yield session


async def _add(session, obj):
    session.add(obj)
    await session.commit()
    await session.refresh(obj)
    return obj


async def _seed(session: AsyncSession):
    def paper(code, year, set_code):
        return Exam(code=code, name=f"UPSC ({year})", source_year=year, paper_set_code=set_code,
                    exam_type=QuestionSourceType.PYQ, is_active=True)
    a2022 = await _add(session, paper("U22A", 2022, "SET_A"))
    b2022 = await _add(session, paper("U22B", 2022, "SET_B"))
    a2019 = await _add(session, paper("U19A", 2019, "SET_A"))
    bank = await _add(session, Exam(code="BANK", name="Mixed bank", source_year=2000, is_mixed_bank=True, is_active=True))

    def q(exam, n, **kw):
        # Similar questions are only suggested within the same subject, so every seeded question has one
        return Question(exam_id=exam.id, question_number_in_paper=n, status=QuestionStatus.PUBLISHED,
                        subject_raw="Polity", **kw)
    target = await _add(session, q(a2022, 1, knowledge_tags_ai=["polity"]))
    same_paper = await _add(session, q(a2022, 2))
    same_year_other_set = await _add(session, q(b2022, 1))
    other_year = await _add(session, q(a2019, 1))
    bank_2022 = await _add(session, q(bank, 1, source_year=2022))
    bank_2015 = await _add(session, q(bank, 2, source_year=2015))
    for n in (3, 4):
        await _add(session, q(b2022, n))
    u = await _add(session, User(full_name="S", email="s@sets.test", role=UserRole.STUDENT, password_hash=get_password_hash("x")))
    _state.update(target=target.id, same_paper=same_paper.id, same_year=same_year_other_set.id, other_year=other_year.id,
                  bank_2022=bank_2022.id, bank_2015=bank_2015.id, user=u.id)


@pytest.fixture
async def client():
    app.dependency_overrides[get_async_session] = _override_session
    await get_cache().delete_pattern("*")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        await c.get("/api/v1/exam-types")
        yield c
    app.dependency_overrides.pop(get_async_session, None)


def _h() -> dict:
    return {"Authorization": f"Bearer {create_access_token(subject=_state['user'], role='STUDENT')}"}


@pytest.mark.anyio
async def test_same_theme_in_other_years_only(client, monkeypatch):
    s = _state
    candidates = [s["same_paper"], s["same_year"], s["other_year"], s["bank_2022"], s["bank_2015"]]

    async def fake_similar(self, qid, limit=5):
        return [
            {"question_id": str(c), "similarity_score": 90 - i, "subject_name": "Polity"}
            for i, c in enumerate(candidates)
        ][:limit]

    monkeypatch.setattr("app.services.question_service.QuestionService.get_similar_questions", fake_similar)
    url = f"/api/v1/questions/{s['target']}/similar"
    every = (await client.get(url, params={"limit": 5})).json()["results"]
    assert len(every) == 5  # unchanged without the option
    other = (await client.get(url, params={"limit": 5, "other_years_only": "true"})).json()["results"]
    # Not the same paper, not 2022 (incl. a mixed-bank question tagged 2022); a bank question's own year counts
    assert [int(r["question_id"]) for r in other] == [s["other_year"], s["bank_2015"]]
    assert [r["source_year"] for r in other] == [2019, 2015]


@pytest.mark.anyio
async def test_subject_wise_paper_set_filter(client):
    url = "/api/v1/attempts/dynamic-question-count"
    all_sets = (await client.get(url, headers=_h())).json()["count"]
    set_b = (await client.get(url, params={"dynamic_sets": "SET_B"}, headers=_h())).json()["count"]
    both = (await client.get(url, params={"dynamic_sets": "set_a,SET_B"}, headers=_h())).json()["count"]
    assert (set_b, both) == (3, 6) and all_sets >= both  # set codes are case-insensitive; a mixed bank never matches a set
    assert (await client.get(url, params={"dynamic_sets": "SET A!"}, headers=_h())).status_code == 422


@pytest.mark.anyio
async def test_similar_questions_taxonomy_filtering(client, monkeypatch):
    s = _state
    # Monkeypatch get_similar_questions to return questions with different taxonomies
    candidates = [
        {"question_id": str(s["other_year"]), "similarity_score": 95, "subject_name": "Indian Polity and Governance", "topic_name": "Parliament", "subtopic_name": "Money Bill"},
        {"question_id": str(s["bank_2022"]), "similarity_score": 90, "subject_name": "Polity", "topic_name": "Parliament", "subtopic_name": "Money Bill"},
        {"question_id": str(s["bank_2015"]), "similarity_score": 85, "subject_name": "Modern History", "topic_name": "Revolts", "subtopic_name": "1857"},
    ]

    async def fake_similar(self, qid, limit=50):
        return candidates

    monkeypatch.setattr("app.services.question_service.QuestionService.get_similar_questions", fake_similar)

    # Target subject "Indian Polity & Governance": only that exact subject name counts ("&" = "and"),
    # so "Polity" (another subject name) and "Modern History" are both excluded
    from app.db.models.question import Question

    async with AsyncSession(test_engine) as session:
        t_q = await session.get(Question, s["target"])
        t_q.subject_raw = "Indian Polity & Governance"
        t_q.topic_raw = "Parliament"
        t_q.subtopic_raw = "Money Bill"
        session.add(t_q)
        await session.commit()

    url = f"/api/v1/questions/{s['target']}/similar"
    res = (await client.get(url, params={"limit": 5})).json()["results"]

    # Only the exact-subject question is kept
    assert len(res) == 1
    assert int(res[0]["question_id"]) == s["other_year"]
    assert res[0]["subject_name"] == "Indian Polity and Governance"
    assert res[0]["subtopic_name"] == "Money Bill"



@pytest.mark.anyio
async def test_similar_exclude_ids_are_never_returned(client, monkeypatch):
    """The page sends its session's question IDs; the server leaves them out before choosing results."""
    s = _state
    candidates = [s["same_paper"], s["same_year"], s["other_year"], s["bank_2022"], s["bank_2015"]]

    async def fake_similar(self, qid, limit=5):
        return [
            {"question_id": str(c), "similarity_score": 90 - i, "subject_name": "Polity"}
            for i, c in enumerate(candidates)
        ][:limit]

    monkeypatch.setattr("app.services.question_service.QuestionService.get_similar_questions", fake_similar)
    async with AsyncSession(test_engine) as session:
        t_q = await session.get(Question, s["target"])
        t_q.subject_raw, t_q.topic_raw, t_q.subtopic_raw = "Polity", None, None
        session.add(t_q)
        await session.commit()

    url = f"/api/v1/questions/{s['target']}/similar"
    excluded = [s["same_paper"], s["other_year"]]
    res = (await client.get(url, params={"limit": 5, "exclude_ids": ",".join(map(str, excluded))})).json()["results"]
    ids = [int(r["question_id"]) for r in res]
    assert ids == [s["same_year"], s["bank_2022"], s["bank_2015"]]


@pytest.mark.anyio
async def test_similar_count_comes_from_the_admin_setting(client, monkeypatch):
    """Without a `limit`, the number of results is the admin's Similar Questions setting."""
    from app.services.site_settings_service import SiteSettingsService

    s = _state
    candidates = [s["same_paper"], s["same_year"], s["other_year"], s["bank_2022"], s["bank_2015"]]

    async def fake_similar(self, qid, limit=5):
        return [
            {"question_id": str(c), "similarity_score": 90 - i, "subject_name": "Polity"}
            for i, c in enumerate(candidates)
        ][:limit]

    monkeypatch.setattr("app.services.question_service.QuestionService.get_similar_questions", fake_similar)
    url = f"/api/v1/questions/{s['target']}/similar"
    async with AsyncSession(test_engine) as session:
        await SiteSettingsService(session).set_similar_questions_limit(2, updated_by_id=None)
    try:
        assert len((await client.get(url)).json()["results"]) == 2
        # An explicit limit still wins (the admin explorer uses it)
        assert len((await client.get(url, params={"limit": 4})).json()["results"]) == 4
    finally:
        async with AsyncSession(test_engine) as session:
            await SiteSettingsService(session).set_similar_questions_limit(6, updated_by_id=None)
    assert len((await client.get(url)).json()["results"]) == 5  # only 5 candidates exist


@pytest.mark.anyio
async def test_similar_setting_is_admin_only_and_bounded(client):
    url = "/api/v1/site-settings/similar-questions"
    # Students can neither read nor change it
    assert (await client.get(url, headers=_h())).status_code == 403
    assert (await client.put(url, json={"similar_questions_limit": 3}, headers=_h())).status_code == 403

    async with AsyncSession(test_engine) as session:
        admin = User(full_name="A", email="admin@sets.test", role=UserRole.ADMIN, password_hash=get_password_hash("x"))
        session.add(admin)
        await session.commit()
        await session.refresh(admin)
    admin_h = {"Authorization": f"Bearer {create_access_token(subject=admin.id, role='ADMIN')}"}
    assert (await client.put(url, json={"similar_questions_limit": 0}, headers=admin_h)).status_code == 422
    assert (await client.put(url, json={"similar_questions_limit": 21}, headers=admin_h)).status_code == 422
    saved = await client.put(url, json={"similar_questions_limit": 8}, headers=admin_h)
    assert saved.status_code == 200 and saved.json()["similar_questions_limit"] == 8
    got = (await client.get(url, headers=admin_h)).json()
    assert got["similar_questions_limit"] == 8 and got["min_limit"] == 1 and got["max_limit"] == 20
    assert (await client.put(url, json={"similar_questions_limit": 6}, headers=admin_h)).status_code == 200
