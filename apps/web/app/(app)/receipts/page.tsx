'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useEffect, useMemo, useState } from 'react';

import {
  ReceiptFieldEditor,
  type ReceiptDraft,
  draftFromReceipt,
} from '@/components/receipts/field-editor';
import { MatchCandidates } from '@/components/receipts/match-candidates';
import { ReceiptImage } from '@/components/receipts/receipt-image';
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
  Spinner,
} from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { cn } from '@/lib/cn';
import { formatShortDate } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';
import type { ReceiptSummary } from '@/lib/types';

function moneyToCents(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const parsed = Number.parseFloat(trimmed.replace(',', '.'));
  return Number.isNaN(parsed) ? undefined : Math.round(parsed * 100);
}

export default function ReceiptsPage() {
  const queryClient = useQueryClient();
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draft, setDraft] = useState<ReceiptDraft | null>(null);
  const [notice, setNotice] = useState<{ tone: 'ok' | 'error'; text: string } | null>(null);
  const [linkingId, setLinkingId] = useState<string | null>(null);

  const listQuery = useQuery({
    queryKey: queryKeys.receipts(),
    queryFn: () => api.receipts(),
  });

  const receipts = useMemo(() => listQuery.data ?? [], [listQuery.data]);

  // Open the first receipt that still needs attention.
  useEffect(() => {
    if (selectedId || receipts.length === 0) return;
    const pending = receipts.find((item) => item.status !== 'confirmed');
    setSelectedId((pending ?? receipts[0]).id);
  }, [receipts, selectedId]);

  const detailQuery = useQuery({
    queryKey: selectedId ? queryKeys.receipt(selectedId) : ['receipt', 'none'],
    queryFn: () => api.receipt(selectedId!),
    enabled: selectedId !== null,
  });

  useEffect(() => {
    if (detailQuery.data) setDraft(draftFromReceipt(detailQuery.data));
  }, [detailQuery.data]);

  const candidatesQuery = useQuery({
    queryKey: selectedId ? queryKeys.matchCandidates(selectedId) : ['match', 'none'],
    queryFn: () => api.matchCandidates(selectedId!),
    enabled: selectedId !== null && detailQuery.data?.status !== 'confirmed',
  });

  const save = useMutation({
    mutationFn: async () => {
      if (!selectedId || !draft) return;
      await api.updateReceipt(selectedId, {
        merchant: draft.merchant || undefined,
        posted_date: draft.posted_date || undefined,
        subtotal_cents: moneyToCents(draft.subtotal),
        tax_cents: moneyToCents(draft.tax),
        tip_cents: moneyToCents(draft.tip),
        total_cents: moneyToCents(draft.total),
        currency: draft.currency || undefined,
      });
    },
    onSuccess: () => {
      setNotice({ tone: 'ok', text: 'Corrections saved.' });
      void queryClient.invalidateQueries({ queryKey: queryKeys.receipt(selectedId!) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.matchCandidates(selectedId!) });
    },
    onError: (error) =>
      setNotice({ tone: 'error', text: error instanceof Error ? error.message : 'Save failed.' }),
  });

  const confirm = useMutation({
    mutationFn: async (input: { mode: 'create' | 'link'; transactionId?: string }) => {
      if (!selectedId || !draft) throw new Error('No receipt selected');
      if (input.mode === 'create') {
        // Persist the user's edits before turning them into a transaction.
        await save.mutateAsync();
      }
      return api.confirmReceipt(selectedId, {
        mode: input.mode,
        account_id: input.mode === 'create' && draft.accountId ? draft.accountId : undefined,
        category_id: input.mode === 'create' && draft.categoryId ? draft.categoryId : undefined,
        transaction_id: input.transactionId,
      });
    },
    onSuccess: (result) => {
      setNotice({ tone: 'ok', text: result.message });
      setLinkingId(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.receiptsAll });
      void queryClient.invalidateQueries({ queryKey: queryKeys.transactionsAll });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
      void queryClient.invalidateQueries({ queryKey: queryKeys.facets });
    },
    onError: (error) => {
      setLinkingId(null);
      setNotice({
        tone: 'error',
        text: error instanceof Error ? error.message : 'Could not confirm this receipt.',
      });
    },
  });

  const reject = useMutation({
    mutationFn: (transactionId: string) => api.rejectCandidate(selectedId!, transactionId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.matchCandidates(selectedId!) }),
  });

  const receipt = detailQuery.data;
  const pendingCount = receipts.filter((item) => item.status !== 'confirmed').length;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Receipts</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Check what was read from each receipt, then create a transaction or link it to one
            you already imported.
          </p>
        </div>
        <Link href="/upload" className="btn-secondary">
          Upload a receipt
        </Link>
      </header>

      {notice ? (
        <p
          role="status"
          className={cn(
            'rounded-lg border px-3 py-2 text-sm',
            notice.tone === 'error'
              ? 'border-negative/30 bg-negative/5 text-negative'
              : 'border-line bg-surface-sunken text-ink-muted',
          )}
        >
          {notice.text}
        </p>
      ) : null}

      {listQuery.isLoading ? (
        <Skeleton className="h-64" />
      ) : receipts.length === 0 ? (
        <Card>
          <EmptyState
            title="No receipts yet"
            description="Upload a JPEG, PNG or PDF receipt and Ledger AI will read the merchant, date and totals from it."
            action={
              <Link href="/upload" className="btn-primary">
                Upload a receipt
              </Link>
            }
          />
        </Card>
      ) : (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,280px)_minmax(0,1fr)]">
          <section aria-label="Receipt queue">
            <Card>
              <CardHeader
                title="Queue"
                subtitle={`${pendingCount} awaiting review`}
              />
              <ul className="divide-y divide-line">
                {receipts.map((item: ReceiptSummary) => (
                  <li key={item.id}>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedId(item.id);
                        setNotice(null);
                      }}
                      aria-current={item.id === selectedId ? 'true' : undefined}
                      className={cn(
                        'w-full px-4 py-3 text-left transition-colors hover:bg-surface-sunken',
                        item.id === selectedId && 'bg-brand-soft/40',
                      )}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-sm font-medium text-ink">
                          {item.merchant ?? item.original_filename}
                        </span>
                        {item.status === 'confirmed' ? (
                          <Badge tone="positive">done</Badge>
                        ) : item.status === 'needs_review' ? (
                          <Badge tone="caution">review</Badge>
                        ) : item.status === 'failed' ? (
                          <Badge tone="negative">failed</Badge>
                        ) : (
                          <Badge tone="brand">ready</Badge>
                        )}
                      </div>
                      <p className="mt-0.5 truncate text-xs text-ink-muted">
                        {item.posted_date ? formatShortDate(item.posted_date) : 'no date'} ·{' '}
                        {item.total !== null
                          ? `${item.total.toFixed(2)} ${item.currency}`
                          : 'no total'}
                      </p>
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
          </section>

          <section aria-label="Receipt detail" className="space-y-4">
            {detailQuery.isLoading || !receipt || !draft ? (
              <Skeleton className="h-96" />
            ) : detailQuery.isError ? (
              <Card>
                <ErrorState
                  message={(detailQuery.error as Error).message}
                  onRetry={() => void detailQuery.refetch()}
                />
              </Card>
            ) : (
              <>
                {receipt.currency_warning ? (
                  <div
                    role="alert"
                    className="rounded-lg border border-caution/40 bg-caution/5 px-4 py-3"
                  >
                    <p className="text-sm font-medium text-caution">
                      Different currency
                    </p>
                    <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                      {receipt.currency_warning}
                    </p>
                  </div>
                ) : null}

                {receipt.status === 'confirmed' ? (
                  <div className="rounded-lg border border-line bg-surface-sunken px-4 py-3">
                    <Badge tone="positive">Confirmed</Badge>
                    <p className="mt-2 text-sm text-ink">
                      This receipt was{' '}
                      {receipt.link_mode === 'linked'
                        ? 'linked to an existing transaction'
                        : 'turned into a new transaction'}
                      .
                    </p>
                    <Link
                      href="/transactions"
                      className="mt-1 inline-block text-xs font-medium text-brand hover:underline"
                    >
                      View in transactions
                    </Link>
                  </div>
                ) : null}

                <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
                  <Card className="p-4">
                    <h2 className="mb-3 text-sm font-semibold text-ink">Original receipt</h2>
                    <ReceiptImage
                      receiptId={receipt.id}
                      pageCount={receipt.page_count}
                      isPdf={receipt.content_type === 'application/pdf'}
                    />
                  </Card>

                  <Card className="p-4">
                    <h2 className="mb-3 text-sm font-semibold text-ink">Extracted fields</h2>
                    <ReceiptFieldEditor
                      receipt={receipt}
                      draft={draft}
                      onChange={(patch) =>
                        setDraft((current) => (current ? { ...current, ...patch } : current))
                      }
                      accounts={receipt.accounts}
                      categories={receipt.categories}
                    />

                    {receipt.status !== 'confirmed' ? (
                      <div className="mt-5 flex flex-wrap gap-2 border-t border-line pt-4">
                        <button
                          type="button"
                          onClick={() => confirm.mutate({ mode: 'create' })}
                          disabled={confirm.isPending || !draft.total || !draft.merchant}
                          className="btn-primary"
                        >
                          {confirm.isPending && linkingId === null ? <Spinner /> : null}
                          Create transaction
                        </button>
                        <button
                          type="button"
                          onClick={() => save.mutate()}
                          disabled={save.isPending}
                          className="btn-secondary"
                        >
                          Save corrections
                        </button>
                      </div>
                    ) : null}
                  </Card>
                </div>

                {receipt.status !== 'confirmed' ? (
                  <MatchCandidates
                    candidates={candidatesQuery.data?.candidates ?? []}
                    isLoading={candidatesQuery.isLoading}
                    note={candidatesQuery.data?.note ?? ''}
                    linkingId={linkingId}
                    onLink={(transactionId) => {
                      setLinkingId(transactionId);
                      confirm.mutate({ mode: 'link', transactionId });
                    }}
                    onReject={(transactionId) => reject.mutate(transactionId)}
                  />
                ) : null}
              </>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
