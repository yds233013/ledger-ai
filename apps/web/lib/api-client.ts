/**
 * Browser-side API client.
 *
 * Calls FastAPI directly with a bearer token obtained from /api/auth/token.
 * The token is cached in memory and refreshed shortly before it expires, so a
 * long session never fails mid-interaction.
 */
import type {
  Capabilities,
  CorrectionImpact,
  Dashboard,
  Facets,
  Page,
  Profile,
  RunSummary,
  Transaction,
  TransactionUpdateResult,
  Upload,
} from './types';

export const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

const REFRESH_MARGIN_MS = 60_000;

let cachedToken: { value: string; expiresAt: number } | null = null;
let inFlight: Promise<string> | null = null;

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

async function fetchToken(): Promise<string> {
  const response = await fetch('/api/auth/token', { cache: 'no-store' });
  if (!response.ok) {
    void handleUnauthorized();
    throw new ApiError('Your session has expired. Redirecting you to sign in…', 401);
  }
  const data = (await response.json()) as { access_token: string; expires_in: number };
  cachedToken = {
    value: data.access_token,
    expiresAt: Date.now() + data.expires_in * 1000,
  };
  return data.access_token;
}

export async function getAccessToken(): Promise<string> {
  if (cachedToken && cachedToken.expiresAt - REFRESH_MARGIN_MS > Date.now()) {
    return cachedToken.value;
  }
  // Collapse concurrent refreshes into one request.
  inFlight ??= fetchToken().finally(() => {
    inFlight = null;
  });
  return inFlight;
}

export function clearTokenCache(): void {
  cachedToken = null;
}

/**
 * A 401 here means the browser session no longer maps to a real user — the
 * session was revoked, or the account is gone. Retrying can never succeed, so
 * clear the stale session and send the user to sign in rather than stranding
 * them on a "Try again" button that is guaranteed to fail.
 */
let redirecting = false;

async function handleUnauthorized(): Promise<void> {
  clearTokenCache();
  if (redirecting || typeof window === 'undefined') return;
  redirecting = true;
  const { signOut } = await import('next-auth/react');
  await signOut({ callbackUrl: '/sign-in' });
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await getAccessToken();
  const headers = new Headers(init.headers);
  headers.set('Authorization', `Bearer ${token}`);
  if (init.body && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }

  let response: Response;
  try {
    response = await fetch(`${API_URL}${path}`, { ...init, headers });
  } catch {
    throw new ApiError(
      'Could not reach the Ledger AI API. Check that the backend is running.',
      0,
    );
  }

  if (response.status === 401) {
    void handleUnauthorized();
    throw new ApiError('Your session has expired. Redirecting you to sign in…', 401);
  }
  if (!response.ok) {
    let detail = `Request failed (${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON error body — keep the generic message */
    }
    throw new ApiError(detail, response.status);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface TransactionQuery {
  search?: string;
  start_date?: string;
  end_date?: string;
  account_id?: string;
  category_slug?: string;
  merchant?: string;
  review?: 'needs_review' | 'corrected' | 'reviewed';
  min_amount?: number;
  max_amount?: number;
  sort?: 'date' | 'amount' | 'merchant' | 'confidence';
  order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
}

function toQueryString(query: object): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query) as [string, unknown][]) {
    if (value !== undefined && value !== null && value !== '') {
      params.set(key, String(value));
    }
  }
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}

export const api = {
  dashboard: () => request<Dashboard>('/api/dashboard'),

  transactions: (query: TransactionQuery = {}) =>
    request<Page<Transaction>>(`/api/transactions${toQueryString(query)}`),

  facets: () => request<Facets>('/api/transactions/facets'),

  updateTransaction: (
    id: string,
    body: { merchant?: string; category_id?: string; apply_to_matching?: boolean },
  ) =>
    request<TransactionUpdateResult>(`/api/transactions/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  /** How many other transactions a bulk correction would change. */
  correctionImpact: (id: string, params: { category_id?: string; merchant?: string }) =>
    request<CorrectionImpact>(
      `/api/transactions/${id}/correction-impact${toQueryString(params)}`,
    ),

  uploads: () => request<Upload[]>('/api/uploads'),

  uploadJob: (uploadId: string) =>
    request<Upload['job']>(`/api/uploads/${uploadId}/job`),

  createUpload: async (file: File): Promise<Upload> => {
    const form = new FormData();
    form.append('file', file);
    return request<Upload>('/api/uploads', { method: 'POST', body: form });
  },

  capabilities: () => request<Capabilities>('/api/analysis/capabilities'),

  analysisRuns: () => request<RunSummary[]>('/api/analysis/runs'),

  profile: () => request<Profile>('/api/settings/profile'),
};
