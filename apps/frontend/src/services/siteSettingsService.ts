import { guardedFetch } from '../utils/apiGuard';

const API_BASE_URL = '/api/v1/site-settings';

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
    'Authorization': `Bearer ${token || ''}`,
    'Content-Type': 'application/json',
  };
}

export interface ThemeSettings {
  theme_name: string;
  updated_at: string;
}

interface ThemeCacheEntry {
  data: ThemeSettings;
  timestamp: number;
}

let activeThemeCache: ThemeCacheEntry | null = null;
const THEME_CACHE_TTL_MS = 10 * 60 * 1000; // 10-minute client cache

/**
 * Purges the in-memory active theme client cache.
 */
export function invalidateActiveThemeCache(): void {
  activeThemeCache = null;
}

export async function fetchActiveTheme(options?: { forceRefresh?: boolean }): Promise<ThemeSettings> {
  const now = Date.now();
  if (!options?.forceRefresh && activeThemeCache && now - activeThemeCache.timestamp < THEME_CACHE_TTL_MS) {
    return activeThemeCache.data;
  }

  const res = await guardedFetch(`${API_BASE_URL}/theme`);
  if (!res.ok) {
    throw new Error('Could not fetch active theme.');
  }
  const data: ThemeSettings = await res.json();
  activeThemeCache = { data, timestamp: now };
  return data;
}

export async function updateActiveTheme(themeName: string): Promise<ThemeSettings> {
  const res = await guardedFetch(`${API_BASE_URL}/theme`, {
    method: 'PUT',
    headers: getAuthHeader(),
    body: JSON.stringify({ theme_name: themeName }),
  });
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({ detail: 'Failed to update theme' }));
    throw new Error(errorData.detail || 'Could not update theme.');
  }
  const data: ThemeSettings = await res.json();
  // Invalidate in-memory cache upon successful mutation
  invalidateActiveThemeCache();
  return data;
}

export interface SimilarQuestionsSettings {
  /** How many similar questions students see per question */
  similar_questions_limit: number;
  min_limit: number;
  max_limit: number;
  updated_at: string | null;
}

export async function fetchSimilarQuestionsSettings(): Promise<SimilarQuestionsSettings> {
  const res = await guardedFetch(`${API_BASE_URL}/similar-questions`, { headers: getAuthHeader() });
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({ detail: 'Failed to load the setting' }));
    throw new Error(errorData.detail || 'Could not load the similar questions setting.');
  }
  return res.json();
}

export async function updateSimilarQuestionsSettings(limit: number): Promise<SimilarQuestionsSettings> {
  const res = await guardedFetch(`${API_BASE_URL}/similar-questions`, {
    method: 'PUT',
    headers: getAuthHeader(),
    body: JSON.stringify({ similar_questions_limit: limit }),
  });
  if (!res.ok) {
    const errorData = await res.json().catch(() => ({ detail: 'Failed to save the setting' }));
    const detail = Array.isArray(errorData.detail) ? errorData.detail[0]?.msg : errorData.detail;
    throw new Error(detail || 'Could not save the similar questions setting.');
  }
  return res.json();
}
