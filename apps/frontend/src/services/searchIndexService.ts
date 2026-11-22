import { guardedFetch } from '../utils/apiGuard';

/** Admin → Similar Questions → Search index (backend: app/routers/search_index.py). */
const API_BASE_URL = '/api/v1/admin/search-index';

function getAuthHeader() {
  let token = localStorage.getItem('access_token');
  if (!token) {
    const storedUser = localStorage.getItem('user');
    if (storedUser) {
      try {
        const parsed = JSON.parse(storedUser);
        token = parsed.access_token || parsed.token || null;
      } catch (e) {}
    }
  }
  return {
    Authorization: `Bearer ${token || ''}`,
    'Content-Type': 'application/json',
  };
}

export type RunStatus = 'queued' | 'running' | 'succeeded' | 'warning' | 'failed';

/** "vectors": embeddings + BM25 (Gemini). "similar_lists": the precomputed lists. "full": both (older runs). */
export type RunKind = 'vectors' | 'similar_lists' | 'full';
/** How similar lists are ordered: fused ranking only ("rrf") or the Jina reranker ("rerank"). */
export type Ranking = 'rrf' | 'rerank';

export interface IndexPlan {
  algo_version: string;
  kind?: RunKind;
  ranking?: Ranking;
  totals: {
    questions: number;
    published_with_subject: number;
    published_without_subject: number;
    subjects: number;
    stored_lists: number;
    stored_lists_by_ranking?: { rrf: number; rerank: number; outdated: number };
  };
  pending: {
    question_embeddings: number;
    solution_embeddings: number;
    embeddings_not_built?: number;
    subjects_out_of_date: number;
    lists_to_compute: number;
    out_of_date_subjects: { subject: string; published_questions: number }[];
  };
  estimate: {
    gemini: { requests: number; texts: number; approx_tokens: number };
    jina: { rerank_calls: number; approx_tokens: number };
    approx_minutes: number;
    minutes?: { vectors: number; similar_lists: number };
    note: string;
  };
  up_to_date: boolean;
}

export interface EmbeddingProgress {
  to_process?: number;
  processed?: number;
  question_embedded?: number;
  question_failed?: number;
  solution_embedded?: number;
  solution_failed?: number;
}

export interface ListProgress {
  ranking?: Ranking;
  subjects_to_update?: number;
  subjects_done?: number;
  lists_total?: number;
  lists_computed?: number;
  lists_reused?: number;
  lists_removed?: number;
  lists_failed?: number;
  current_subject?: string;
  failed_examples?: { question_id: number; mode: string; code: string }[];
}

export interface IndexRun {
  id: number;
  status: RunStatus;
  trigger: 'cli' | 'admin' | 'upload';
  triggered_by_id: number | null;
  resumed_from_id: number | null;
  phase: 'embeddings' | 'bm25' | 'similar_lists' | 'done' | null;
  options: { kind?: RunKind; use_reranker?: boolean; [key: string]: unknown };
  estimate: Partial<Pick<IndexPlan, 'pending' | 'estimate' | 'totals'>>;
  progress: { embeddings?: EmbeddingProgress; bm25?: Record<string, unknown>; similar_lists?: ListProgress };
  summary: {
    ranking?: Ranking;
    questions_embedded?: number;
    solutions_embedded?: number;
    embedding_failures?: number;
    subjects_updated?: number;
    lists_computed?: number;
    lists_reused?: number;
    lists_removed?: number;
    lists_failed?: number;
    warning?: string;
  };
  error_code: string | null;
  error_message: string | null;
  created_at: string | null;
  started_at: string | null;
  heartbeat_at: string | null;
  finished_at: string | null;
}

export interface IndexStatus {
  /** What a vectors run would do (embeddings to make, Gemini estimate). */
  vectors_plan: IndexPlan;
  /** What a similar-lists run would do with the ranking asked for (`use_reranker`). */
  lists_plan: IndexPlan;
  /** Same as lists_plan; kept for older clients. */
  plan: IndexPlan;
  use_reranker: boolean;
  active_run: IndexRun | null;
  last_success_at: string | null;
  recent_runs: IndexRun[];
  use_precomputed: boolean;
  live_use_reranker: boolean;
  gemini_key_configured: boolean;
  jina_key_configured: boolean;
}

export interface StartRunOptions {
  kind: RunKind;
  use_reranker?: boolean;
  force?: boolean;
}

async function errorText(res: Response, fallback: string): Promise<string> {
  const data = await res.json().catch(() => ({}));
  const detail = (data as any).detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail.message === 'string') return detail.message;
  return fallback;
}

/** `useReranker` only changes the similar-lists estimate (lists_plan); nothing is started. */
export async function fetchSearchIndexStatus(useReranker = false): Promise<IndexStatus> {
  const res = await guardedFetch(`${API_BASE_URL}/status?use_reranker=${useReranker}`, { headers: getAuthHeader() });
  if (!res.ok) throw new Error(await errorText(res, 'Could not load the search index status.'));
  return res.json();
}

export async function startSearchIndexRun(options: StartRunOptions): Promise<IndexRun> {
  const res = await guardedFetch(`${API_BASE_URL}/runs`, {
    method: 'POST',
    headers: getAuthHeader(),
    body: JSON.stringify(options),
  });
  if (!res.ok) throw new Error(await errorText(res, 'Could not start the run.'));
  return res.json();
}

export async function fetchSearchIndexRun(id: number): Promise<IndexRun> {
  const res = await guardedFetch(`${API_BASE_URL}/runs/${id}`, { headers: getAuthHeader() });
  if (!res.ok) throw new Error(await errorText(res, 'Could not load the run.'));
  return res.json();
}
