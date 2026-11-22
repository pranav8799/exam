"""
Backend-only audit of GET /api/v1/questions/{id}/similar against the configured database.

It calls the real endpoint in-process (no frontend, no running server) for every question and checks
the rules the feature promises. Use it after a migration, a backfill or any change to the search.

Checks per question, in three scenarios (normal, other_years_only, whole paper sent as exclude_ids):
  - HTTP 200
  - every result has exactly the candidate's subject (app/core/subjects.py)
  - every result is PUBLISHED, is not the candidate, appears once
  - nothing from exclude_ids; with other_years_only nothing from the same paper or year
  - the card's subject_name matches too
  - the number of results = the admin setting, or fewer only when the subject has fewer candidates
Also reports response times and how many responses came from the fallback paths.

Usage (from apps/backend):
  # Rules audit of every question; the Jina reranker is replaced by a no-op (it only reorders
  # results and would otherwise cost one paid call per request)
  python -m app.scripts.audit_similar_questions

  # Real reranker on N random published questions: true response times + a relevance read-out
  python -m app.scripts.audit_similar_questions --sample 30 --out relevance_sample.md
"""
import argparse
import asyncio
import collections
import logging
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

QUESTION_INFO_SQL = """
select q.id, cast(q.status as text), coalesce(sj.name, nullif(btrim(q.subject_raw), '')) as subj,
       q.exam_id, coalesce(q.source_year, e.source_year) as yr, e.name,
       left(regexp_replace(coalesce(qt.question_text, ''), '\\s+', ' ', 'g'), 160)
from questions q join exams e on e.id = q.exam_id
left join subtopics st on st.id = q.subtopic_id left join topics t on t.id = st.topic_id
left join subjects sj on sj.id = t.subject_id
left join lateral (select question_text from question_translations x where x.question_id = q.id
                   order by (cast(x.language as text) = 'ENGLISH') desc limit 1) qt on true
"""


def _percentile(values, p):
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, round(p / 100 * (len(ordered) - 1))))]


async def run(sample: int, out: str) -> int:
    logging.disable(logging.WARNING)
    from app.services.vector import gemini_embedder, jina_reranker

    if not sample:
        async def _rerank(self, query, documents, top_n=None, **kw):
            docs = documents[:top_n] if top_n else documents
            return [{"index": i, "relevance_score": 1.0 - i * 0.01} for i in range(len(docs))]
        jina_reranker.JinaReranker.rerank = _rerank

    async def _no_embed(self, *a, **kw):  # audit stored vectors only; never call Gemini
        return None
    gemini_embedder.GeminiEmbedder.embed_text = _no_embed

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text
    from app.core.deps import require_question_visible
    from app.core.subjects import normalize_subject
    from app.db.session import async_session_factory
    from app.fastapi_server import app

    app.dependency_overrides[require_question_visible] = lambda: None  # include unpublished candidates

    async with async_session_factory() as s:
        rows = (await s.exec(text(QUESTION_INFO_SQL))).fetchall()
        limit = (await s.exec(text("select similar_questions_limit from site_settings where id = 1"))).first()[0]
    Q = {r[0]: dict(status=r[1], subj=normalize_subject(r[2]), subj_name=r[2], exam=r[3], year=r[4], paper=r[5], text=r[6])
         for r in rows}
    published = [i for i, q in Q.items() if q["status"] == "PUBLISHED"]
    by_subject = collections.defaultdict(list)
    for i in published:
        if Q[i]["subj"]:
            by_subject[Q[i]["subj"]].append(i)
    papers = collections.defaultdict(list)
    for i, q in Q.items():
        papers[q["exam"]].append(i)
    print(f"questions={len(Q)} published={len(published)} subjects={len(by_subject)} papers={len(papers)} admin_limit={limit}")

    def candidate_pool(qid, other_years=False, exclude=()):
        t = Q[qid]
        if not t["subj"]:
            return []
        out_ids = [o for o in by_subject[t["subj"]] if o != qid and o not in exclude]
        if other_years:
            out_ids = [o for o in out_ids if Q[o]["exam"] != t["exam"] and (t["year"] is None or Q[o]["year"] != t["year"])]
        return out_ids

    targets = random.Random(7).sample(published, min(sample, len(published))) if sample else sorted(Q)
    stats = collections.Counter()
    problems = []
    times = collections.defaultdict(list)
    samples = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://audit", timeout=120) as client:
        for n, qid in enumerate(targets, 1):
            t = Q[qid]
            scenarios = [("default", {}, ()), ("other_years", {"other_years_only": "true"}, ())]
            if not sample:
                session = [o for o in papers[t["exam"]] if o != qid]
                scenarios.append(("session_excluded", {"exclude_ids": ",".join(map(str, session))}, tuple(session)))
            for name, extra, excluded in scenarios:
                started = time.perf_counter()
                resp = await client.get(f"/api/v1/questions/{qid}/similar", params={"include_question": "true", **extra})
                times[name].append(time.perf_counter() - started)
                if resp.status_code != 200:
                    stats[f"{name}:http_{resp.status_code}"] += 1
                    problems.append((name, qid, f"HTTP {resp.status_code}"))
                    continue
                res = resp.json().get("results", [])
                ids = [int(x["question_id"]) for x in res]
                pool = candidate_pool(qid, other_years=(name == "other_years"), exclude=set(excluded))
                expected = min(limit, len(pool))
                stats[f"{name}:with_results" if ids else f"{name}:empty"] += 1
                if any(not any("Matched via" in m for m in x.get("match_reasons", [])) for x in res):
                    stats[f"{name}:served_by_fallback"] += 1

                def bad(msg, _name=name, _qid=qid):
                    problems.append((_name, _qid, msg))

                if len(ids) != len(set(ids)):
                    bad("duplicate results")
                if qid in ids:
                    bad("returned itself")
                for o in ids:
                    if o not in Q:
                        bad(f"unknown id {o}")
                        continue
                    if Q[o]["subj"] != t["subj"]:
                        bad(f"subject {Q[o]['subj_name']!r} != {t['subj_name']!r} (Q{o})")
                    if Q[o]["status"] != "PUBLISHED":
                        bad(f"unpublished Q{o}")
                    if o in excluded:
                        bad(f"excluded Q{o} returned")
                    if name == "other_years" and (Q[o]["exam"] == t["exam"] or (t["year"] is not None and Q[o]["year"] == t["year"])):
                        bad(f"same paper/year Q{o}")
                    card = (next(x for x in res if int(x["question_id"]) == o).get("question") or {}).get("subject_name")
                    if card is not None and normalize_subject(card) != t["subj"]:
                        bad(f"card subject {card!r}")
                if len(ids) != expected:
                    bad(f"count {len(ids)} != expected {expected} (pool {len(pool)})")
                if sample and name == "default":
                    samples.append((qid, t, [(x["question_id"], x.get("similarity_score"), Q.get(int(x["question_id"]), {}).get("text", ""))
                                              for x in res]))
            if not sample and n % 250 == 0:
                print(f"  ... {n}/{len(targets)} questions, {len(problems)} problems so far", flush=True)

    print("results:", dict(stats))
    for name, values in times.items():
        print(f"time[{name}] n={len(values)} p50={_percentile(values, 50) * 1000:.0f}ms "
              f"p95={_percentile(values, 95) * 1000:.0f}ms max={max(values) * 1000:.0f}ms")
    print(f"problems: {len(problems)}")
    for p in problems[:25]:
        print("   ", p)
    if sample:
        with open(out, "w", encoding="utf-8") as f:
            f.write("# Similar questions: relevance sample (real reranker)\n\n")
            for qid, t, res in samples:
                f.write(f"## Q{qid} [{t['subj_name']}] {t['paper']}\n{t['text']}\n\n")
                for rid, score, txt in res:
                    f.write(f"- **{score}%** Q{rid}: {txt}\n")
                f.write("\n")
        print("relevance sample written to", out)
    return 1 if problems else 0


def main():
    parser = argparse.ArgumentParser(description="Backend-only audit of similar-question suggestions")
    parser.add_argument("--sample", type=int, default=0,
                        help="Audit N random published questions with the real reranker (default: every question, no reranker)")
    parser.add_argument("--out", default="relevance_sample.md", help="Where --sample writes its relevance read-out")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.sample, args.out)))


if __name__ == "__main__":
    main()
