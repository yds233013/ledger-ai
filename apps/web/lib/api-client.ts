/**
 * Browser-side API client.
 *
 * Calls FastAPI directly with a bearer token obtained from /api/auth/token.
 * The token is cached in memory and refreshed shortly before it expires, so a
 * long session never fails mid-interaction.
 */
import type {
  AlertList,
  Capabilities,
  ConfirmResponse,
  ConsentState,
  CorrectionImpact,
  DeletionResult,
  Dashboard,
  Facets,
  Page,
  MatchCandidatesResponse,
  Profile,
  ReceiptDetail,
  ReceiptSummary,
  RunSummary,
  Transaction,
  TransactionUpdateResult,
  Upload,
  Usage,
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

async function handleUnauthorized(reason?: 'demo-expired'): Promise<void> {
  clearTokenCache();
  if (redirecting || typeof window === 'undefined') return;
  redirecting = true;
  const { signOut } = await import('next-auth/react');
  // The reason travels in the URL so the sign-in page can explain what
  // happened. A demo that ends is not the same event as a session that was
  // revoked, and being told which one it was is the difference between
  // "expected" and "broken".
  const callbackUrl = reason === 'demo-expired' ? '/sign-in?demo=expired' : '/sign-in';
  await signOut({ callbackUrl });
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
    let detail = '';
    try {
      const body = await response.clone().json();
      if (typeof body?.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON body — fall through to the generic path */
    }
    const demoExpired = detail.toLowerCase().includes('demo session has ended');
    void handleUnauthorized(demoExpired ? 'demo-expired' : undefined);
    throw new ApiError(
      demoExpired ? detail : 'Your session has expired. Redirecting you to sign in…',
      401,
    );
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
  /**
   * Only transactions carrying an open alert.
   *
   * Deliberately separate from `review`: an alerted charge is often
   * categorized with full confidence and so is absent from the review queue.
   */
  flagged?: boolean;
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

/**
 * Fetch an authorized binary asset as an object URL.
 *
 * The receipt image endpoint requires a bearer token, so it cannot be used as
 * a plain <img src>. The caller must revoke the URL when done.
 */
export async function fetchAuthorizedObjectUrl(path: string): Promise<string> {
  const token = await getAccessToken();
  const response = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: 'no-store',
  });
  if (!response.ok) {
    throw new ApiError(`Could not load that file (${response.status}).`, response.status);
  }
  return URL.createObjectURL(await response.blob());
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

  consents: () => request<ConsentState>('/api/settings/consents'),

  acceptConsents: (consentTypes: string[]) =>
    request<ConsentState>('/api/settings/consents', {
      method: 'POST',
      body: JSON.stringify({ consent_types: consentTypes }),
      headers: { 'Content-Type': 'application/json' },
    }),

  usage: () => request<Usage>('/api/settings/usage'),

  /* --- receipts --------------------------------------------------------- */

  receipts: (statusFilter?: string) =>
    request<ReceiptSummary[]>(
      `/api/receipts${toQueryString({ status_filter: statusFilter })}`,
    ),

  receipt: (id: string) => request<ReceiptDetail>(`/api/receipts/${id}`),

  /** Authorized image endpoint. Never a public URL. */
  receiptImageUrl: (id: string, page = 1) =>
    `${API_URL}/api/receipts/${id}/image?page=${page}`,

  updateReceipt: (
    id: string,
    body: Partial<{
      merchant: string;
      posted_date: string;
      subtotal_cents: number;
      tax_cents: number;
      tip_cents: number;
      total_cents: number;
      currency: string;
    }>,
  ) =>
    request<ReceiptDetail>(`/api/receipts/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),

  matchCandidates: (id: string) =>
    request<MatchCandidatesResponse>(`/api/receipts/${id}/match-candidates`),

  rejectCandidate: (id: string, transactionId: string) =>
    request<void>(`/api/receipts/${id}/reject-candidate`, {
      method: 'POST',
      body: JSON.stringify({ transaction_id: transactionId }),
    }),

  confirmReceipt: (
    id: string,
    body: {
      mode: 'create' | 'link';
      account_id?: string;
      category_id?: string;
      transaction_id?: string;
    },
  ) =>
    request<ConfirmResponse>(`/api/receipts/${id}/confirm`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  /* --- data lifecycle --------------------------------------------------- */

  /**
   * Download the export.
   *
   * Fetched as a blob rather than linked directly: the endpoint needs the
   * bearer token, which an <a href> cannot carry.
   */
  exportData: async (): Promise<{ blob: Blob; filename: string }> => {
    const token = await getAccessToken();
    const response = await fetch(`${API_URL}/api/settings/export`, {
      headers: { Authorization: `Bearer ${token}` },
      cache: 'no-store',
    });
    if (!response.ok) {
      throw new ApiError(
        response.status === 429
          ? 'You have requested several exports recently. Please wait a few minutes.'
          : `Export failed (${response.status}).`,
        response.status,
      );
    }
    const disposition = response.headers.get('content-disposition') ?? '';
    const match = /filename="([^"]+)"/.exec(disposition);
    return { blob: await response.blob(), filename: match?.[1] ?? 'ledgerai-export.zip' };
  },

  deleteData: (dryRun = false) =>
    request<DeletionResult>('/api/settings/delete-data', {
      method: 'POST',
      body: JSON.stringify({ confirmation: 'DELETE', dry_run: dryRun }),
    }),

  deleteAccount: (dryRun = false) =>
    request<DeletionResult>('/api/settings/delete-account', {
      method: 'POST',
      body: JSON.stringify({ confirmation: 'DELETE', dry_run: dryRun }),
    }),

  deleteReceipt: (id: string) =>
    request<{ message: string }>(`/api/receipts/${id}`, { method: 'DELETE' }),

  /* --- alerts ----------------------------------------------------------- */

  alerts: (statusFilter: string = 'open') =>
    request<AlertList>(`/api/alerts${toQueryString({ status_filter: statusFilter })}`),

  updateAlert: (id: string, status: 'open' | 'dismissed' | 'resolved') =>
    request<unknown>(`/api/alerts/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
};
