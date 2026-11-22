import React, { useEffect, useState } from 'react';
import { Layers } from 'lucide-react';
import {
  fetchSimilarQuestionsSettings,
  updateSimilarQuestionsSettings,
} from '../../../services/siteSettingsService';
import { useBreakpoint } from '../../../hooks/useBreakpoint';
import { SearchIndexPanel } from './SearchIndexPanel';

/**
 * Admin → Similar Questions: how many similar questions students see for each question
 * (practice pages and the post-exam "Same theme in other years" section).
 */
export function SimilarQuestionsSettingsView() {
  const { isMobile } = useBreakpoint();
  const [limit, setLimit] = useState<number | ''>('');
  const [savedLimit, setSavedLimit] = useState<number | null>(null);
  const [bounds, setBounds] = useState({ min: 1, max: 20 });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState<{ text: string; isError: boolean } | null>(null);

  const showToast = (text: string, isError = false) => {
    setToast({ text, isError });
    setTimeout(() => setToast(null), 4000);
  };

  useEffect(() => {
    fetchSimilarQuestionsSettings()
      .then((data) => {
        setLimit(data.similar_questions_limit);
        setSavedLimit(data.similar_questions_limit);
        setBounds({ min: data.min_limit, max: data.max_limit });
      })
      .catch((err: any) => showToast(err.message || 'Could not load the setting.', true))
      .finally(() => setLoading(false));
  }, []);

  const isValid = typeof limit === 'number' && limit >= bounds.min && limit <= bounds.max;
  const isDirty = isValid && limit !== savedLimit;

  const handleSave = async () => {
    if (!isValid || saving) return;
    setSaving(true);
    try {
      const data = await updateSimilarQuestionsSettings(limit as number);
      setSavedLimit(data.similar_questions_limit);
      setLimit(data.similar_questions_limit);
      showToast(`Students will now see up to ${data.similar_questions_limit} similar questions.`);
    } catch (err: any) {
      showToast(err.message || 'Could not save the setting.', true);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      style={{
        background: 'var(--bg-card)',
        border: '1px solid var(--border-color)',
        borderRadius: 'var(--radius)',
        padding: isMobile ? '20px 16px' : 32,
        boxShadow: '0 12px 36px -10px rgba(15, 29, 56, 0.08)',
        fontFamily: 'Inter, sans-serif',
      }}
    >
      <div style={{ marginBottom: 24 }}>
        <h2 style={{ fontSize: 22, fontWeight: 800, color: 'var(--text-main)', margin: 0, letterSpacing: '-0.02em' }}>
          Similar Questions
        </h2>
        <p style={{ fontSize: 14, color: 'var(--text-muted)', margin: '4px 0 0 0' }}>
          How many similar questions students see for each question. They always come from the same subject
          and never include questions already in the student&apos;s session.
        </p>
      </div>

      {toast && (
        <div
          role="status"
          style={{
            padding: '12px 16px',
            marginBottom: 16,
            borderRadius: 'var(--radius)',
            fontSize: 13.5,
            fontWeight: 600,
            background: toast.isError ? 'rgba(227, 27, 35, 0.12)' : 'rgba(16, 185, 129, 0.12)',
            color: toast.isError ? 'var(--primary-red)' : '#3A8B53',
            border: `1px solid ${toast.isError ? 'rgba(227,27,35,0.3)' : 'rgba(16,185,129,0.3)'}`,
          }}
        >
          {toast.text}
        </div>
      )}

      <div
        style={{
          border: '1px solid var(--border-color)',
          borderRadius: 'var(--radius)',
          padding: 20,
          maxWidth: 520,
          display: 'flex',
          flexDirection: 'column',
          gap: 14,
          background: 'var(--bg-body, #ffffff)',
        }}
      >
        <label
          htmlFor="similar-questions-limit"
          style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 15, fontWeight: 700, color: 'var(--text-main)' }}
        >
          <Layers size={16} aria-hidden="true" />
          Similar questions per question
        </label>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
          <input
            id="similar-questions-limit"
            type="number"
            min={bounds.min}
            max={bounds.max}
            step={1}
            value={limit}
            disabled={loading}
            onChange={(e) => {
              const v = e.target.value;
              setLimit(v === '' ? '' : Math.trunc(Number(v)));
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') handleSave();
            }}
            style={{
              width: 96,
              padding: '10px 12px',
              fontSize: 16,
              fontWeight: 700,
              borderRadius: 'var(--radius)',
              border: `1px solid ${isValid || limit === '' ? 'var(--border-color)' : 'var(--primary-red)'}`,
              background: 'var(--bg-card)',
              color: 'var(--text-main)',
            }}
          />
          <button
            type="button"
            onClick={handleSave}
            disabled={!isDirty || saving || loading}
            style={{
              padding: '10px 18px',
              fontSize: 14,
              fontWeight: 700,
              borderRadius: 'var(--radius)',
              border: 'none',
              cursor: !isDirty || saving || loading ? 'not-allowed' : 'pointer',
              background: !isDirty || saving || loading ? 'var(--border-color)' : 'var(--accent-primary, #e31b23)',
              color: !isDirty || saving || loading ? 'var(--text-muted)' : '#fff',
            }}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
        <p style={{ fontSize: 12.5, color: isValid || limit === '' ? 'var(--text-muted)' : 'var(--primary-red)', margin: 0 }}>
          {isValid || limit === ''
            ? `Between ${bounds.min} and ${bounds.max}. Fewer may appear when a subject has few other questions.`
            : `Enter a whole number between ${bounds.min} and ${bounds.max}.`}
        </p>
      </div>

      <SearchIndexPanel />
    </div>
  );
}
