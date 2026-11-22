import { useEffect, useState } from 'react';
import type { QuestionOut } from '../types/exam';
import { guardedFetch } from '../utils/apiGuard';

/**
 * "Similar questions" for one question (GET /questions/{id}/similar).
 *
 * Found and ranked on the backend, only among published questions of exactly the same subject
 * (see docs/SIMILAR_QUESTIONS.md). The backend also decides how many to return.
 */
export type SimilarTier = 'exact' | 'related' | 'nano_theme' | 'microtheme';

export interface SimilarQuestionResult {
  question_id: number;
  tier: SimilarTier;
  similarity_score?: number;
  match_reasons: string[];
  question_number?: number | null;
  exam_name?: string | null;
  source_year?: number | null;
  subject_name?: string | null;
  topic_name?: string | null;
  subtopic_name?: string | null;
  question?: any | null;
}

/** A recommendation card tagged with how closely it matched. */
export type SimilarQuestionOut = QuestionOut & {
  similar_tier?: SimilarTier;
  similarity_score?: number;
  match_reasons?: string[];
};

/** Maps a question card embedded in a /similar result to the QuestionOut used by the panels. */
export function recommendationCardToQuestion(item: any, idx: number): QuestionOut {
  const optionsList = (item.options || []).map((o: any, oIdx: number) => ({
    option_id: o.option_id || `opt-rec-${idx}-${o.option_label || String.fromCharCode(65 + oIdx)}`,
    option_label: o.option_label || String.fromCharCode(65 + oIdx),
    text: o.option_text || o.text || `Option ${o.option_label || String.fromCharCode(65 + oIdx)}`,
    // Cards don't carry the answer key; correctness comes from /questions/{id}/solution after answering.
    distractor_analysis: o.distractor_analysis || o.explanation || o.description || '',
    translations: o.translations || {},
  }));
  // A question without a subject keeps it blank: never invent one
  const subject = item.subject_name || '';
  return {
    question_id: item.question_id,
    question_number: 0,
    text: item.question_text || item.text || '',
    passage_text: item.passage_text || null,
    translations: item.translations || {},
    passage_translations: item.passage_translations || {},
    options: optionsList,
    section_code: subject,
    subject,
    subject_name: subject,
    topic_name: item.topic_name || '',
    subtopic_name: item.subtopic_name,
    difficulty: item.difficulty || 'MEDIUM',
    marks: item.marks || 2.0,
    negative_marks: item.negative_marks || 0.66,
    expected_time_seconds: item.expected_time_seconds || 60,
    source_year: item.source_year || item.exam_year,
    exam_name: item.exam_name,
  } as any;
}

export async function fetchSimilarQuestions(
  questionId: number | string,
  options: {
    /** Omit to use the admin setting (Admin → Similar Questions) */
    limit?: number;
    language?: string;
    includeQuestion?: boolean;
    otherYearsOnly?: boolean;
    /** Never return these (e.g. the current session); removed on the server before ranking */
    excludeIds?: Iterable<string | number>;
  } = {},
): Promise<SimilarQuestionResult[]> {
  const params = new URLSearchParams();
  if (options.limit) params.append('limit', String(options.limit));
  if (options.includeQuestion) params.append('include_question', 'true');
  if (options.otherYearsOnly) params.append('other_years_only', 'true');
  if (options.language) params.append('language', options.language);
  const excluded = Array.from(options.excludeIds || []).map(String).filter((id) => /^\d+$/.test(id));
  if (excluded.length) params.append('exclude_ids', excluded.join(','));
  const res = await guardedFetch(`/api/v1/questions/${questionId}/similar?${params.toString()}`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.results || [];
}

/** Numeric DB ids only — guest/demo placeholder questions have no knowledge data. */
function isRealQuestionId(id: unknown): boolean {
  return id !== null && id !== undefined && /^\d+$/.test(String(id));
}

/**
 * Similar questions for the question currently on screen, as ready-to-render panel cards.
 * How many is decided by the backend (admin setting); the session's own questions are sent as
 * `exclude_ids` so the server fills every slot with questions the student hasn't got on screen.
 * Cached per question id + language + excluded set, so moving back and forth doesn't refetch.
 */
export function useSimilarQuestions(
  questionId: number | string | null | undefined,
  language: string | undefined,
  excludeIds: Set<string>,
  /** Only questions from other years (the post-test "same theme in other years" section) */
  otherYearsOnly = false,
): { similar: SimilarQuestionOut[]; isLoading: boolean } {
  const [cache, setCache] = useState<Record<string, SimilarQuestionOut[]>>({});
  const [loadingKey, setLoadingKey] = useState<string | null>(null);
  const excludedKey = Array.from(excludeIds).sort().join(',');
  const key = isRealQuestionId(questionId)
    ? `${questionId}:${language || 'en'}${otherYearsOnly ? ':other-years' : ''}:${excludedKey}`
    : null;

  useEffect(() => {
    if (!key || cache[key]) return;
    let cancelled = false;
    setLoadingKey(key);
    fetchSimilarQuestions(questionId as number | string, {
      language,
      includeQuestion: true,
      otherYearsOnly,
      excludeIds,
    })
      .then((results) => {
        if (cancelled) return;
        const cards = results
          .filter((r) => r.question)
          .map((r, idx) => ({
            ...recommendationCardToQuestion(r.question, idx),
            similar_tier: r.tier,
            similarity_score: r.similarity_score,
            match_reasons: r.match_reasons || [],
          }));
        setCache((prev) => ({ ...prev, [key]: cards }));
      })
      .catch(() => {
        if (!cancelled) setCache((prev) => ({ ...prev, [key]: [] }));
      })
      .finally(() => {
        if (!cancelled) setLoadingKey((current) => (current === key ? null : current));
      });
    return () => {
      cancelled = true;
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  // Safety net only: the server already left these out
  const similar = (key ? cache[key] || [] : []).filter((q) => !excludeIds.has(String(q.question_id)));
  return { similar, isLoading: key !== null && loadingKey === key };
}
