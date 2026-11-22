import React, { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Database, ListOrdered, Loader2, Play, RefreshCw, XCircle } from 'lucide-react';
import {
  fetchSearchIndexStatus,
  startSearchIndexRun,
  type IndexPlan,
  type IndexRun,
  type IndexStatus,
  type RunKind,
  type RunStatus,
} from '../../../services/searchIndexService';

/**
 * Admin → Similar Questions → Search index, in two independent steps:
 *  1. Build vectors: embeddings (Gemini) + BM25 statistics. Done once, then only for new or edited questions.
 *  2. Build similar lists from those vectors, ranked either by the fused ranking (RRF, no paid calls) or by the
 *     Jina reranker. Can be rebuilt as often as needed to compare the two, without paying for vectors again.
 * During a run: phase and progress (polled). After: the run's report, or why it stopped.
 * Runs are checkpointed in the database, so starting again after a failure resumes it.
 */

const POLL_MS = 3000;

const PHASES: { key: NonNullable<IndexRun['phase']>; label: string }[] = [
  { key: 'embeddings', label: 'Embeddings' },
  { key: 'bm25', label: 'BM25 statistics' },
  { key: 'similar_lists', label: 'Similar lists' },
  { key: 'done', label: 'Done' },
];

/** The phases a run of this kind goes through. */
function phasesFor(kind: RunKind | undefined) {
  if (kind === 'vectors') return PHASES.filter((p) => p.key !== 'similar_lists');
  if (kind === 'similar_lists') return PHASES.filter((p) => p.key !== 'embeddings');
  return PHASES;
}

const STATUS_STYLE: Record<RunStatus, { label: string; color: string; bg: string }> = {
  queued: { label: 'Queued', color: '#6B7280', bg: 'rgba(107,114,128,0.12)' },
  running: { label: 'Running', color: '#2563EB', bg: 'rgba(37,99,235,0.12)' },
  succeeded: { label: 'Succeeded', color: '#3A8B53', bg: 'rgba(16,185,129,0.12)' },
  warning: { label: 'Finished with warnings', color: '#B45309', bg: 'rgba(245,158,11,0.14)' },
  failed: { label: 'Failed', color: 'var(--primary-red)', bg: 'rgba(227,27,35,0.12)' },
};

const TRIGGER_LABEL: Record<IndexRun['trigger'], string> = { cli: 'Script', admin: 'Admin', upload: 'PDF upload' };

/** "Vectors", "Similar lists (no reranker)", "Similar lists (Jina reranker)" or "Vectors + lists". */
function runTypeLabel(run: IndexRun): string {
  const kind = run.options?.kind ?? 'full';
  const ranking = run.options?.use_reranker ? 'Jina reranker' : 'no reranker';
  if (kind === 'vectors') return 'Vectors';
  if (kind === 'similar_lists') return `Similar lists (${ranking})`;
  return run.options?.skip_similar ? 'Vectors' : `Vectors + lists (${ranking})`;
}

const fmt = (n: number | undefined | null) => (n ?? 0).toLocaleString();

function when(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

function duration(run: IndexRun): string {
  if (!run.started_at) return '—';
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const sec = Math.max(0, Math.round((end.getTime() - new Date(run.started_at).getTime()) / 1000));
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  return min < 60 ? `${min}m ${sec % 60}s` : `${Math.floor(min / 60)}h ${min % 60}m`;
}

const card: React.CSSProperties = {
  border: '1px solid var(--border-color)',
  borderRadius: 'var(--radius)',
  padding: 16,
  background: 'var(--bg-body, #ffffff)',
};

const sectionTitle: React.CSSProperties = {
  fontSize: 12,
  fontWeight: 800,
  letterSpacing: '0.06em',
  textTransform: 'uppercase',
  color: 'var(--text-muted)',
  margin: '0 0 10px 0',
};

const statGrid: React.CSSProperties = { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 12 };

function Stat({ label, value, hint, tone }: { label: string; value: string; hint?: string; tone?: 'warn' }) {
  return (
    <div style={{ minWidth: 0 }}>
      <div style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 800, color: tone === 'warn' ? '#B45309' : 'var(--text-main)', fontVariantNumeric: 'tabular-nums' }}>
        {value}
      </div>
      {hint && <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{hint}</div>}
    </div>
  );
}

function StatusBadge({ status }: { status: RunStatus }) {
  const s = STATUS_STYLE[status];
  return (
    <span style={{ padding: '3px 10px', borderRadius: 999, fontSize: 12, fontWeight: 700, color: s.color, background: s.bg, whiteSpace: 'nowrap' }}>
      {s.label}
    </span>
  );
}

function Notice({ children, tone = 'warn' }: { children: React.ReactNode; tone?: 'warn' | 'info' }) {
  const warn = tone === 'warn';
  return (
    <div
      style={{
        display: 'flex',
        gap: 8,
        alignItems: 'flex-start',
        padding: '10px 12px',
        borderRadius: 'var(--radius)',
        background: warn ? 'rgba(245,158,11,0.12)' : 'rgba(37,99,235,0.08)',
        color: warn ? '#92400E' : 'var(--text-main)',
        fontSize: 13,
      }}
    >
      <AlertTriangle size={15} style={{ flexShrink: 0, marginTop: 1 }} aria-hidden="true" />
      <span>{children}</span>
    </div>
  );
}

function Bar({ done, total, label }: { done: number; total: number; label: string }) {
  const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 100;
  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13, marginBottom: 4, color: 'var(--text-main)' }}>
        <span>{label}</span>
        <span style={{ fontVariantNumeric: 'tabular-nums' }}>
          {fmt(done)} / {fmt(total)} ({pct}%)
        </span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuenow={pct}
        aria-valuemin={0}
        aria-valuemax={100}
        style={{ height: 8, borderRadius: 999, background: 'var(--border-color)', overflow: 'hidden' }}
      >
        <div style={{ width: `${pct}%`, height: '100%', background: 'var(--accent-primary, #e31b23)', transition: 'width .4s' }} />
      </div>
    </div>
  );
}

function StartButton({
  label,
  onClick,
  disabled,
  busy,
}: {
  label: string;
  onClick: () => void;
  disabled: boolean;
  busy: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '10px 18px',
        fontSize: 14,
        fontWeight: 700,
        borderRadius: 'var(--radius)',
        border: 'none',
        cursor: disabled ? 'not-allowed' : 'pointer',
        background: disabled ? 'var(--border-color)' : 'var(--accent-primary, #e31b23)',
        color: disabled ? 'var(--text-muted)' : '#fff',
      }}
    >
      {busy ? <Loader2 size={15} className="animate-spin" aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}
      {label}
    </button>
  );
}

function RunProgress({ run }: { run: IndexRun }) {
  const emb = run.progress.embeddings;
  const lists = run.progress.similar_lists;
  const phases = phasesFor(run.options?.kind);
  const current = phases.findIndex((p) => p.key === run.phase);
  return (
    <div style={{ ...card, display: 'flex', flexDirection: 'column', gap: 14 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <Loader2 size={16} className="animate-spin" aria-hidden="true" />
        <strong style={{ color: 'var(--text-main)' }}>
          Run #{run.id} in progress · {runTypeLabel(run)}
        </strong>
        <StatusBadge status={run.status} />
        <span style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>
          {TRIGGER_LABEL[run.trigger]} · started {when(run.started_at || run.created_at)} · {duration(run)}
        </span>
      </div>
      <ol style={{ display: 'flex', gap: 8, flexWrap: 'wrap', listStyle: 'none', padding: 0, margin: 0 }}>
        {phases.map((p, i) => (
          <li
            key={p.key}
            style={{
              fontSize: 12.5,
              fontWeight: 700,
              padding: '4px 10px',
              borderRadius: 999,
              color: i === current ? '#fff' : i < current ? '#3A8B53' : 'var(--text-muted)',
              background: i === current ? '#2563EB' : i < current ? 'rgba(16,185,129,0.12)' : 'var(--border-color)',
            }}
          >
            {i + 1}. {p.label}
          </li>
        ))}
      </ol>
      {emb && (emb.to_process ?? 0) > 0 && <Bar label="Questions embedded" done={emb.processed ?? 0} total={emb.to_process ?? 0} />}
      {lists && (
        <>
          <Bar label="Similar lists" done={(lists.lists_computed ?? 0) + (lists.lists_reused ?? 0)} total={lists.lists_total ?? 0} />
          <div style={{ fontSize: 13, color: 'var(--text-muted)' }}>
            Subjects {fmt(lists.subjects_done)} / {fmt(lists.subjects_to_update)}
            {lists.current_subject ? ` · now: ${lists.current_subject}` : ''}
            {(lists.lists_reused ?? 0) > 0 ? ` · ${fmt(lists.lists_reused)} kept from the previous run` : ''}
          </div>
        </>
      )}
    </div>
  );
}

function RunReport({ run }: { run: IndexRun }) {
  const s = run.summary || {};
  const failed = run.status === 'failed';
  const kind = run.options?.kind ?? 'full';
  const showVectors = kind !== 'similar_lists';
  const showLists = kind !== 'vectors' && !run.options?.skip_similar;
  return (
    <div
      style={{
        ...card,
        borderColor: failed ? 'rgba(227,27,35,0.35)' : run.status === 'warning' ? 'rgba(245,158,11,0.4)' : 'rgba(16,185,129,0.35)',
        display: 'flex',
        flexDirection: 'column',
        gap: 10,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        {failed ? (
          <XCircle size={16} color="var(--primary-red)" aria-hidden="true" />
        ) : run.status === 'warning' ? (
          <AlertTriangle size={16} color="#B45309" aria-hidden="true" />
        ) : (
          <CheckCircle2 size={16} color="#3A8B53" aria-hidden="true" />
        )}
        <strong style={{ color: 'var(--text-main)' }}>
          Last run #{run.id} · {runTypeLabel(run)}
        </strong>
        <StatusBadge status={run.status} />
        <span style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>
          {TRIGGER_LABEL[run.trigger]} · {when(run.finished_at)} · took {duration(run)}
          {run.resumed_from_id ? ` · resumed run #${run.resumed_from_id}` : ''}
        </span>
      </div>
      {failed && (
        <div role="alert" style={{ fontSize: 13.5, color: 'var(--primary-red)', fontWeight: 600 }}>
          Stopped during “{PHASES.find((p) => p.key === run.phase)?.label ?? run.phase}”: {run.error_message}
          {run.error_code && <code style={{ marginLeft: 6, fontSize: 12, opacity: 0.8 }}>[{run.error_code}]</code>}
          <div style={{ fontWeight: 400, color: 'var(--text-muted)', marginTop: 4 }}>
            Fix the cause, then start the run again. Work already finished is kept and is not paid for again.
          </div>
        </div>
      )}
      {s.warning && <div style={{ fontSize: 13.5, color: '#B45309' }}>{s.warning}</div>}
      <div style={statGrid}>
        {showVectors && <Stat label="Questions embedded" value={fmt(s.questions_embedded)} />}
        {showVectors && <Stat label="Solutions embedded" value={fmt(s.solutions_embedded)} />}
        {showLists && <Stat label="Subjects updated" value={fmt(s.subjects_updated)} />}
        {showLists && <Stat label="Lists computed" value={fmt(s.lists_computed)} hint={s.lists_reused ? `${fmt(s.lists_reused)} kept` : undefined} />}
        {showLists && <Stat label="Lists removed" value={fmt(s.lists_removed)} hint="unpublished / moved" />}
        {(s.lists_failed ?? 0) + (s.embedding_failures ?? 0) > 0 && (
          <Stat label="Failed (retried next run)" value={fmt((s.lists_failed ?? 0) + (s.embedding_failures ?? 0))} tone="warn" />
        )}
      </div>
    </div>
  );
}

function OutOfDateSubjects({ plan }: { plan: IndexPlan }) {
  const [open, setOpen] = useState(false);
  if (plan.pending.out_of_date_subjects.length === 0) return null;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        style={{ background: 'none', border: 'none', padding: 0, fontSize: 13, fontWeight: 600, color: 'var(--accent-primary, #e31b23)', cursor: 'pointer' }}
      >
        {open ? 'Hide' : 'Show'} out-of-date subjects
      </button>
      {open && (
        <ul style={{ margin: '8px 0 0 0', paddingLeft: 18, fontSize: 13, color: 'var(--text-main)', columns: '220px auto' }}>
          {plan.pending.out_of_date_subjects.map((s) => (
            <li key={s.subject}>
              {s.subject} <span style={{ color: 'var(--text-muted)' }}>({fmt(s.published_questions)})</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function RankingChoice({
  useReranker,
  onChange,
  jinaAvailable,
  disabled,
}: {
  useReranker: boolean;
  onChange: (value: boolean) => void;
  jinaAvailable: boolean;
  disabled: boolean;
}) {
  const option = (value: boolean, title: string, hint: string, optionDisabled: boolean) => (
    <label
      style={{
        display: 'flex',
        gap: 8,
        alignItems: 'flex-start',
        padding: '10px 12px',
        flex: '1 1 240px',
        borderRadius: 'var(--radius)',
        border: `1px solid ${useReranker === value ? 'var(--accent-primary, #e31b23)' : 'var(--border-color)'}`,
        cursor: optionDisabled ? 'not-allowed' : 'pointer',
        opacity: optionDisabled ? 0.55 : 1,
      }}
    >
      <input
        type="radio"
        name="similar-lists-ranking"
        checked={useReranker === value}
        onChange={() => onChange(value)}
        disabled={optionDisabled}
        style={{ marginTop: 3 }}
      />
      <span>
        <span style={{ display: 'block', fontSize: 13.5, fontWeight: 700, color: 'var(--text-main)' }}>{title}</span>
        <span style={{ display: 'block', fontSize: 12.5, color: 'var(--text-muted)' }}>{hint}</span>
      </span>
    </label>
  );
  return (
    <fieldset style={{ border: 'none', padding: 0, margin: 0 }}>
      <legend style={{ ...sectionTitle, padding: 0 }}>Ranking</legend>
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        {option(false, 'Without reranker', 'Order and score from the fused ranking of the five searches (RRF). No paid API calls.', disabled)}
        {option(
          true,
          'With Jina reranker',
          jinaAvailable ? 'Top fused candidates reranked by Jina. Paid; paced to the key’s limits.' : 'Needs JINA_API_KEY in the backend .env.',
          disabled || !jinaAvailable,
        )}
      </div>
    </fieldset>
  );
}

export function SearchIndexPanel() {
  const [status, setStatus] = useState<IndexStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState<RunKind | null>(null);
  const [useReranker, setUseReranker] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchSearchIndexStatus(useReranker);
      setStatus(data);
      setError(null);
      return data;
    } catch (err: any) {
      setError(err.message || 'Could not load the search index status.');
      return null;
    }
  }, [useReranker]);

  // Load (again when the ranking choice changes) and poll while a run is active
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      const data = await load();
      if (!cancelled && data?.active_run) timer.current = setTimeout(tick, POLL_MS);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [load, status?.active_run?.id]);

  const vectors = status?.vectors_plan;
  const lists = status?.lists_plan;
  const active = status?.active_run ?? null;
  const last = status?.recent_runs.find((r) => r.status !== 'queued' && r.status !== 'running') ?? null;
  const byRanking = lists?.totals.stored_lists_by_ranking;
  const vectorsMissing = lists?.pending.embeddings_not_built ?? 0;

  const start = async (kind: RunKind, confirmText: string) => {
    if (starting || active) return;
    if (!window.confirm(confirmText)) return;
    setStarting(kind);
    try {
      await startSearchIndexRun({ kind, use_reranker: kind === 'similar_lists' ? useReranker : false });
      await load();
    } catch (err: any) {
      setError(err.message || 'Could not start the run.');
      await load();
    } finally {
      setStarting(null);
    }
  };

  const startVectors = () => {
    if (!vectors) return;
    const est = vectors.estimate;
    start(
      'vectors',
      `Build vectors (embeddings and BM25 statistics)?\n\n` +
        `• Gemini: ${fmt(est.gemini.requests)} requests (~${fmt(est.gemini.approx_tokens)} tokens)\n` +
        `• About ${est.minutes?.vectors ?? est.approx_minutes} min\n\n${est.note}`,
    );
  };

  const startLists = () => {
    if (!lists) return;
    const est = lists.estimate;
    const ranking = useReranker ? 'with the Jina reranker' : 'without the reranker (fused ranking)';
    start(
      'similar_lists',
      `Build similar lists ${ranking}?\n\n` +
        `• ${fmt(lists.pending.lists_to_compute)} lists in ${fmt(lists.pending.subjects_out_of_date)} subjects\n` +
        (useReranker ? `• Jina: ${fmt(est.jina.rerank_calls)} rerank calls (~${fmt(est.jina.approx_tokens)} tokens)\n` : '') +
        `• About ${est.minutes?.similar_lists ?? est.approx_minutes} min` +
        (vectorsMissing ? `\n\nNote: ${fmt(vectorsMissing)} text(s) have no vector yet. Build vectors first for the best lists.` : ''),
    );
  };

  const busyLabel = (kind: RunKind, idle: string) => (active ? 'Run in progress' : starting === kind ? 'Starting…' : idle);

  return (
    <section aria-labelledby="search-index-title" style={{ marginTop: 32, display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap' }}>
        <div>
          <h3 id="search-index-title" style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 18, fontWeight: 800, margin: 0, color: 'var(--text-main)' }}>
            <Database size={18} aria-hidden="true" /> Search index
          </h3>
          <p style={{ fontSize: 13.5, color: 'var(--text-muted)', margin: '4px 0 0 0', maxWidth: 680 }}>
            Two separate steps. <strong>1. Vectors</strong> are built once (and again only for new or edited questions).{' '}
            <strong>2. Similar lists</strong> are computed from those vectors ahead of time so pages load instantly; rebuild
            them as often as you like, with or without the reranker. Questions without a stored list use live search.
          </p>
        </div>
        <button
          type="button"
          onClick={() => load()}
          aria-label="Refresh status"
          style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '8px 12px', fontSize: 13, fontWeight: 600, borderRadius: 'var(--radius)', border: '1px solid var(--border-color)', background: 'var(--bg-card)', color: 'var(--text-main)', cursor: 'pointer' }}
        >
          <RefreshCw size={14} aria-hidden="true" /> Refresh
        </button>
      </div>

      {error && (
        <div role="alert" style={{ ...card, color: 'var(--primary-red)', borderColor: 'rgba(227,27,35,0.3)', fontSize: 13.5, fontWeight: 600 }}>
          {error}
        </div>
      )}

      {!status && !error && (
        <div style={{ ...card, display: 'flex', alignItems: 'center', gap: 8, color: 'var(--text-muted)', fontSize: 13.5 }}>
          <Loader2 size={14} className="animate-spin" aria-hidden="true" /> Checking what is out of date…
        </div>
      )}

      {status && vectors && lists && (
        <>
          {/* Current state */}
          <div style={{ ...card, display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8, flexWrap: 'wrap' }}>
              <p style={{ ...sectionTitle, margin: 0 }}>Current state</p>
              <span style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>
                Last successful run: {when(status.last_success_at)}
                {!status.use_precomputed && ' · stored lists are switched off (SIMILARITY_USE_PRECOMPUTED)'}
              </span>
            </div>
            <div style={statGrid}>
              <Stat label="Questions" value={fmt(lists.totals.questions)} />
              <Stat
                label="Published, with subject"
                value={fmt(lists.totals.published_with_subject)}
                hint={lists.totals.published_without_subject ? `${fmt(lists.totals.published_without_subject)} without a subject (no suggestions)` : undefined}
              />
              <Stat label="Subjects" value={fmt(lists.totals.subjects)} />
              <Stat
                label="Stored lists"
                value={fmt((byRanking?.rrf ?? 0) + (byRanking?.rerank ?? 0))}
                hint={`${fmt(byRanking?.rrf)} without reranker · ${fmt(byRanking?.rerank)} reranked${byRanking?.outdated ? ` · ${fmt(byRanking.outdated)} out of date` : ''}`}
              />
            </div>
            <div style={{ fontSize: 12.5, color: 'var(--text-muted)' }}>
              Live searches (questions without a stored list) are ranked {status.live_use_reranker ? 'with the Jina reranker' : 'without the reranker'}{' '}
              (setting SIMILARITY_LIVE_USE_RERANKER).
            </div>
          </div>

          {/* Step 1: vectors */}
          <div style={{ ...card, display: 'flex', flexDirection: 'column', gap: 14 }}>
            <h4 style={{ display: 'flex', alignItems: 'center', gap: 8, margin: 0, fontSize: 15.5, fontWeight: 800, color: 'var(--text-main)' }}>
              <Database size={16} aria-hidden="true" /> 1. Vectors
              <span style={{ fontSize: 12.5, fontWeight: 600, color: vectors.up_to_date ? '#3A8B53' : '#B45309' }}>
                {vectors.up_to_date ? 'Up to date' : 'Out of date'}
              </span>
            </h4>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: 0 }}>
              Embeddings of each question and its solution (Gemini), plus the keyword-search statistics. Runs automatically after a PDF
              upload; only new or changed texts are embedded.
            </p>
            <div style={statGrid}>
              <Stat label="Question embeddings to make" value={fmt(vectors.pending.question_embeddings)} tone={vectors.pending.question_embeddings ? 'warn' : undefined} />
              <Stat label="Solution embeddings to make" value={fmt(vectors.pending.solution_embeddings)} tone={vectors.pending.solution_embeddings ? 'warn' : undefined} />
              <Stat label="Gemini (estimate)" value={`${fmt(vectors.estimate.gemini.requests)} requests`} hint={`~${fmt(vectors.estimate.gemini.approx_tokens)} tokens`} />
              <Stat label="Time (estimate)" value={`~${vectors.estimate.minutes?.vectors ?? vectors.estimate.approx_minutes} min`} />
            </div>
            {!status.gemini_key_configured && <Notice>GEMINI_API_KEY is not set in the backend .env, so vectors cannot be built.</Notice>}
            <div>
              <StartButton
                label={busyLabel('vectors', vectors.up_to_date ? 'Refresh BM25 statistics' : 'Build vectors')}
                onClick={startVectors}
                disabled={!!active || !!starting}
                busy={starting === 'vectors'}
              />
            </div>
          </div>

          {/* Step 2: similar lists */}
          <div style={{ ...card, display: 'flex', flexDirection: 'column', gap: 14 }}>
            <h4 style={{ display: 'flex', alignItems: 'center', gap: 8, margin: 0, fontSize: 15.5, fontWeight: 800, color: 'var(--text-main)' }}>
              <ListOrdered size={16} aria-hidden="true" /> 2. Similar lists
              <span style={{ fontSize: 12.5, fontWeight: 600, color: lists.up_to_date ? '#3A8B53' : '#B45309' }}>
                {lists.up_to_date ? `Up to date (${useReranker ? 'reranked' : 'without reranker'})` : 'Out of date for this ranking'}
              </span>
            </h4>
            <p style={{ fontSize: 13, color: 'var(--text-muted)', margin: 0 }}>
              Uses the vectors from step 1; no embeddings are made. Switching the ranking rebuilds every subject, and students keep
              seeing the current lists until each subject is rebuilt.
            </p>
            <RankingChoice useReranker={useReranker} onChange={setUseReranker} jinaAvailable={status.jina_key_configured} disabled={!!active} />
            <div style={statGrid}>
              <Stat label="Subjects out of date" value={fmt(lists.pending.subjects_out_of_date)} tone={lists.pending.subjects_out_of_date ? 'warn' : undefined} />
              <Stat label="Lists to compute" value={fmt(lists.pending.lists_to_compute)} tone={lists.pending.lists_to_compute ? 'warn' : undefined} />
              {useReranker && (
                <Stat label="Jina (estimate)" value={`${fmt(lists.estimate.jina.rerank_calls)} calls`} hint={`~${fmt(lists.estimate.jina.approx_tokens)} tokens`} />
              )}
              <Stat label="Time (estimate)" value={`~${lists.estimate.minutes?.similar_lists ?? lists.estimate.approx_minutes} min`} />
            </div>
            <OutOfDateSubjects plan={lists} />
            {vectorsMissing > 0 && (
              <Notice>
                {fmt(vectorsMissing)} text(s) have no vector yet. Lists built now leave those out of the meaning-based searches; build vectors
                first for the best lists.
              </Notice>
            )}
            {useReranker && !lists.up_to_date && (
              <Notice>
                {lists.estimate.note} If credits run out during a run, it stops with that reason and resumes from the same point when started
                again.
              </Notice>
            )}
            <div>
              <StartButton
                label={busyLabel('similar_lists', lists.up_to_date ? 'Rebuild anyway' : 'Build similar lists')}
                onClick={startLists}
                disabled={!!active || !!starting}
                busy={starting === 'similar_lists'}
              />
            </div>
          </div>

          {active && <RunProgress run={active} />}
          {!active && last && <RunReport run={last} />}

          {status.recent_runs.length > 0 && (
            <div style={{ ...card, padding: 0, overflowX: 'auto' }}>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                <caption style={{ ...sectionTitle, textAlign: 'left', padding: '14px 16px 0' }}>Recent runs</caption>
                <thead>
                  <tr style={{ textAlign: 'left', color: 'var(--text-muted)' }}>
                    {['#', 'Type', 'Started', 'By', 'Status', 'Took', 'Lists', 'Embedded', 'Reason'].map((h) => (
                      <th key={h} scope="col" style={{ padding: '10px 16px', fontWeight: 700, borderBottom: '1px solid var(--border-color)', whiteSpace: 'nowrap' }}>
                        {h}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {status.recent_runs.map((r) => {
                    const listCount = r.summary.lists_computed ?? r.progress.similar_lists?.lists_computed ?? 0;
                    const embedded = (r.summary.questions_embedded ?? 0) + (r.summary.solutions_embedded ?? 0);
                    return (
                      <tr key={r.id} style={{ borderBottom: '1px solid var(--border-color)', color: 'var(--text-main)' }}>
                        <td style={{ padding: '10px 16px' }}>{r.id}</td>
                        <td style={{ padding: '10px 16px', whiteSpace: 'nowrap' }}>{runTypeLabel(r)}</td>
                        <td style={{ padding: '10px 16px', whiteSpace: 'nowrap' }}>{when(r.started_at || r.created_at)}</td>
                        <td style={{ padding: '10px 16px' }}>{TRIGGER_LABEL[r.trigger]}</td>
                        <td style={{ padding: '10px 16px' }}><StatusBadge status={r.status} /></td>
                        <td style={{ padding: '10px 16px', whiteSpace: 'nowrap' }}>{duration(r)}</td>
                        <td style={{ padding: '10px 16px', fontVariantNumeric: 'tabular-nums' }}>
                          {fmt(listCount)}
                          {r.summary.lists_reused ? <span style={{ color: 'var(--text-muted)' }}> (+{fmt(r.summary.lists_reused)} kept)</span> : null}
                        </td>
                        <td style={{ padding: '10px 16px', fontVariantNumeric: 'tabular-nums' }}>{fmt(embedded)}</td>
                        <td style={{ padding: '10px 16px', color: r.status === 'failed' ? 'var(--primary-red)' : '#B45309', minWidth: 220 }}>
                          {r.error_message || r.summary.warning || ''}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </section>
  );
}
