import type { TransactionQuery } from './api-client';

/** Centralized query keys so invalidation is never a guess. */
export const queryKeys = {
  dashboard: ['dashboard'] as const,
  facets: ['facets'] as const,
  transactions: (query: TransactionQuery) => ['transactions', query] as const,
  transactionsAll: ['transactions'] as const,
  correctionImpact: (id: string, categoryId?: string, merchant?: string) =>
    ['correction-impact', id, categoryId ?? null, merchant ?? null] as const,
  uploads: ['uploads'] as const,
  uploadJob: (id: string) => ['upload-job', id] as const,
  capabilities: ['capabilities'] as const,
  analysisRuns: ['analysis-runs'] as const,
  profile: ['profile'] as const,
  consents: ['consents'] as const,
  usage: ['usage'] as const,
  receipts: (statusFilter?: string) => ['receipts', statusFilter ?? 'all'] as const,
  receiptsAll: ['receipts'] as const,
  receipt: (id: string) => ['receipt', id] as const,
  matchCandidates: (id: string) => ['match-candidates', id] as const,
  alerts: (statusFilter: string) => ['alerts', statusFilter] as const,
  alertsAll: ['alerts'] as const,
};
