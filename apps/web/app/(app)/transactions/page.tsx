'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useState } from 'react';

import { TransactionFilters } from '@/components/transactions/filters';
import {
  type PendingCorrection,
  TransactionRow,
} from '@/components/transactions/transaction-row';
import { Card, EmptyState, ErrorState, Skeleton } from '@/components/ui/primitives';
import { api, type TransactionQuery } from '@/lib/api-client';
import { affectedIds, applyOptimisticCorrection } from '@/lib/corrections';
import { queryKeys } from '@/lib/query-keys';
import type { Page, Transaction } from '@/lib/types';

const PAGE_SIZE = 50;
const DEFAULT_QUERY: TransactionQuery = { limit: PAGE_SIZE, offset: 0, sort: 'date', order: 'desc' };

export default function TransactionsPage() {
  const queryClient = useQueryClient();
  const [query, setQuery] = useState<TransactionQuery>(DEFAULT_QUERY);
  const [savingId, setSavingId] = useState<string | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);

  // The row the user is mid-correction on. Held here rather than in the row so
  // only one confirmation can be open at a time.
  const [pending, setPending] = useState<(PendingCorrection & { id: string }) | null>(null);

  const facetsQuery = useQuery({ queryKey: queryKeys.facets, queryFn: api.facets });

  const listQuery = useQuery({
    queryKey: queryKeys.transactions(query),
    queryFn: () => api.transactions(query),
    placeholderData: (previous) => previous,
  });

  // How many other transactions the change would touch. Fetched before the
  // user confirms so the number they approve is the number that gets written.
  const impactQuery = useQuery({
    queryKey: pending
      ? queryKeys.correctionImpact(pending.id, pending.categoryId)
      : ['correction-impact', 'idle'],
    queryFn: () => api.correctionImpact(pending!.id, { category_id: pending!.categoryId }),
    enabled: pending !== null,
    staleTime: 0,
  });

  const correct = useMutation({
    mutationFn: ({
      id,
      categoryId,
      applyToMatching,
    }: {
      id: string;
      categoryId: string;
      applyToMatching: boolean;
    }) =>
      api.updateTransaction(id, {
        category_id: categoryId,
        apply_to_matching: applyToMatching,
      }),

    // Optimistic update. When the correction is retroactive we already know the
    // exact ids the server will change (from the impact preview), so the whole
    // visible set updates at once — and rolls back to the exact previous page
    // data if the request fails.
    onMutate: async ({ id, categoryId, applyToMatching }) => {
      setSavingId(id);
      setErrorMessage(null);
      const key = queryKeys.transactions(query);
      await queryClient.cancelQueries({ queryKey: key });

      const previous = queryClient.getQueryData<Page<Transaction>>(key);
      const category = facetsQuery.data?.categories.find((item) => item.id === categoryId);

      const affected = affectedIds(id, applyToMatching, impactQuery.data);
      if (previous && category) {
        queryClient.setQueryData<Page<Transaction>>(
          key,
          applyOptimisticCorrection(previous, affected, category),
        );
      }
      return { previous, key };
    },

    onError: (error, _variables, context) => {
      // Restore the exact snapshot rather than refetching, so a failed bulk
      // change never leaves some rows updated and others not.
      if (context?.previous) queryClient.setQueryData(context.key, context.previous);
      setErrorMessage(
        error instanceof Error ? error.message : 'Could not save that change. Please try again.',
      );
    },

    onSettled: () => {
      setSavingId(null);
      setPending(null);
      // The correction changed spending by category and the review count, and
      // it invalidates any cached analysis for this user.
      void queryClient.invalidateQueries({ queryKey: queryKeys.transactionsAll });
      void queryClient.invalidateQueries({ queryKey: queryKeys.facets });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
      void queryClient.invalidateQueries({ queryKey: queryKeys.analysisRuns });
    },
  });

  const handleChange = useCallback((patch: Partial<TransactionQuery>) => {
    setQuery((current) => ({ ...current, ...patch, offset: 0 }));
  }, []);

  const page = listQuery.data;
  const total = page?.total ?? 0;
  const offset = query.offset ?? 0;

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Transactions</h1>
          <p className="mt-1 text-sm text-ink-muted">
            Correcting a category also teaches future imports about that merchant.
          </p>
        </div>
        {facetsQuery.data ? (
          <p className="text-sm text-ink-muted tabular-nums">
            {total.toLocaleString()} of {facetsQuery.data.total_count.toLocaleString()} shown
          </p>
        ) : null}
      </header>

      <Card>
        {facetsQuery.data ? (
          <TransactionFilters
            facets={facetsQuery.data}
            query={query}
            onChange={handleChange}
            onReset={() => setQuery(DEFAULT_QUERY)}
          />
        ) : (
          <div className="p-4">
            <Skeleton className="h-20" />
          </div>
        )}

        {errorMessage ? (
          <p role="alert" className="border-b border-line bg-negative/5 px-4 py-2 text-sm text-negative">
            {errorMessage}
          </p>
        ) : null}

        {listQuery.isLoading ? (
          <div className="space-y-2 p-4">
            {Array.from({ length: 8 }).map((_, index) => (
              <Skeleton key={index} className="h-12" />
            ))}
          </div>
        ) : listQuery.isError ? (
          <ErrorState
            message={(listQuery.error as Error).message}
            onRetry={() => void listQuery.refetch()}
          />
        ) : !page || page.items.length === 0 ? (
          <EmptyState
            title="No transactions match these filters"
            description="Try widening the date range, clearing the category filter, or searching for a different merchant."
            action={
              <button type="button" onClick={() => setQuery(DEFAULT_QUERY)} className="btn-secondary">
                Clear filters
              </button>
            }
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[820px]">
              <caption className="sr-only">
                Transactions, newest first. Use the category menu in each row to correct it.
              </caption>
              <thead>
                <tr className="border-b border-line bg-surface-sunken/60 text-left">
                  <th scope="col" className="px-4 py-2.5 text-xs font-medium text-ink-muted">
                    Date
                  </th>
                  <th scope="col" className="px-4 py-2.5 text-xs font-medium text-ink-muted">
                    Merchant
                  </th>
                  <th scope="col" className="px-4 py-2.5 text-xs font-medium text-ink-muted">
                    Category
                  </th>
                  <th scope="col" className="px-4 py-2.5 text-xs font-medium text-ink-muted">
                    Confidence
                  </th>
                  <th scope="col" className="px-4 py-2.5 text-right text-xs font-medium text-ink-muted">
                    Amount
                  </th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((transaction) => (
                  <TransactionRow
                    key={transaction.id}
                    transaction={transaction}
                    categories={facetsQuery.data?.categories ?? []}
                    isSaving={savingId === transaction.id}
                    pending={pending?.id === transaction.id ? pending : null}
                    impact={pending?.id === transaction.id ? (impactQuery.data ?? null) : null}
                    impactLoading={pending?.id === transaction.id && impactQuery.isFetching}
                    onCategoryChange={(id, categoryId) => {
                      if (!categoryId) return;
                      setErrorMessage(null);
                      // Retroactive by default; the user can turn it off in the
                      // confirmation row before applying.
                      setPending({ id, categoryId, applyToMatching: true });
                    }}
                    onToggleApplyToMatching={(next) =>
                      setPending((current) =>
                        current ? { ...current, applyToMatching: next } : current,
                      )
                    }
                    onCancel={() => setPending(null)}
                    onConfirm={() =>
                      pending &&
                      correct.mutate({
                        id: pending.id,
                        categoryId: pending.categoryId,
                        applyToMatching: pending.applyToMatching,
                      })
                    }
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}

        {page && total > PAGE_SIZE ? (
          <div className="flex items-center justify-between border-t border-line px-4 py-3">
            <p className="text-xs text-ink-muted tabular-nums">
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total.toLocaleString()}
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                disabled={offset === 0}
                onClick={() =>
                  setQuery((current) => ({
                    ...current,
                    offset: Math.max(0, (current.offset ?? 0) - PAGE_SIZE),
                  }))
                }
                className="btn-secondary"
              >
                Previous
              </button>
              <button
                type="button"
                disabled={!page.has_more}
                onClick={() =>
                  setQuery((current) => ({
                    ...current,
                    offset: (current.offset ?? 0) + PAGE_SIZE,
                  }))
                }
                className="btn-secondary"
              >
                Next
              </button>
            </div>
          </div>
        ) : null}
      </Card>
    </div>
  );
}
