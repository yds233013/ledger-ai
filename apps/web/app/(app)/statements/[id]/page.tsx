'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useParams, useRouter } from 'next/navigation';
import { useState } from 'react';

import { ReviewTable } from '@/components/statements/review-table';
import { Badge, Card, CardHeader, ErrorState, Spinner } from '@/components/ui/primitives';
import { ApiError, api } from '@/lib/api-client';
import { formatDate } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';

/** Import-level notes, explained rather than named. */
const NOTE_COPY: Record<string, string> = {
  balance_chain_broken:
    'The running balances do not add up across every row. The rows that break the chain are marked below.',
  no_balance_column:
    'This statement has no running balance, so the amounts could not be cross-checked against one.',
  currency_not_stated: 'The statement does not say which currency it is in.',
  text_layer_disagrees_with_render:
    'The text inside this PDF does not match what its pages display.',
};

export default function StatementReviewPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const { data, isLoading, isError, refetch } = useQuery({
    queryKey: queryKeys.statementImport(id),
    queryFn: () => api.statementImport(id),
  });

  const confirm = useMutation({
    mutationFn: () => api.confirmStatementImport(id),
    onSuccess: (result) => {
      setError(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.statementImport(id) });
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
      void queryClient.invalidateQueries({ queryKey: queryKeys.transactionsAll });
      void queryClient.invalidateQueries({ queryKey: queryKeys.usage });
      if (result.status === 'committed') router.push('/transactions');
    },
    onError: (cause) =>
      setError(cause instanceof ApiError ? cause.message : 'That import could not be confirmed.'),
  });

  const discard = useMutation({
    mutationFn: () => api.discardStatementImport(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.statementImports });
      router.push('/upload');
    },
    onError: (cause) =>
      setError(cause instanceof ApiError ? cause.message : 'That import could not be discarded.'),
  });

  if (isLoading) {
    return (
      <Card className="p-5">
        <Spinner />
      </Card>
    );
  }

  if (isError || !data) {
    return (
      <Card>
        <ErrorState message="That statement import could not be loaded." onRetry={() => void refetch()} />
      </Card>
    );
  }

  const rows = data.rows ?? [];
  const included = rows.filter((row) => !row.excluded);
  const flagged = included.filter((row) => row.confidence < 0.8 && !row.edited);
  const committed = data.status === 'committed';

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Review this statement</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Nothing here is in your transactions yet. Check the rows, untick anything you do not
          want, then import.
        </p>
      </header>

      <Card>
        <CardHeader
          title={
            data.period_start && data.period_end
              ? `${formatDate(data.period_start)} to ${formatDate(data.period_end)}`
              : 'Statement'
          }
          subtitle={`${data.row_count} row(s) read from ${data.table_pages} of ${data.page_count} page(s)`}
          action={
            committed ? (
              <Badge tone="positive">Imported</Badge>
            ) : data.balance_chain_ok ? (
              <Badge tone="positive">Balances check out</Badge>
            ) : data.balance_chain_checked ? (
              <Badge tone="negative">Balances do not add up</Badge>
            ) : (
              <Badge tone="neutral">No balance column</Badge>
            )
          }
        />
        <div className="p-5">
          <dl className="grid grid-cols-2 gap-4 sm:grid-cols-4">
            {[
              ['Rows to import', String(included.length)],
              ['Needing a look', String(flagged.length)],
              ['Lines skipped', String(data.skipped_lines)],
              ['Currency', data.currency ?? 'Not stated'],
            ].map(([label, value]) => (
              <div key={label}>
                <dt className="text-xs text-ink-faint">{label}</dt>
                <dd className="mt-0.5 text-sm font-medium text-ink">{value}</dd>
              </div>
            ))}
          </dl>

          {data.notes.length > 0 ? (
            <ul className="mt-4 space-y-1.5 border-t border-line pt-4 text-xs leading-relaxed text-ink-muted">
              {data.notes.map((note) => (
                <li key={note}>{NOTE_COPY[note] ?? note}</li>
              ))}
            </ul>
          ) : null}

          {!committed ? (
            <p className="mt-4 border-t border-line pt-4 text-xs leading-relaxed text-ink-faint">
              The original PDF is deleted as soon as you import, and automatically if you do not
              come back &mdash; this import expires {formatDate(data.expires_at)}.
            </p>
          ) : null}
        </div>
      </Card>

      <Card>
        <CardHeader title="Transactions read from this statement" />
        <ReviewTable importId={id} rows={rows} editable={!committed} />
      </Card>

      {error ? (
        <p role="alert" className="text-sm text-negative">
          {error}
        </p>
      ) : null}

      {!committed ? (
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            className="btn-primary"
            disabled={confirm.isPending || included.length === 0}
            onClick={() => confirm.mutate()}
            data-testid="confirm-import"
          >
            {confirm.isPending ? <Spinner /> : null}
            {confirm.isPending
              ? 'Importing…'
              : `Import ${included.length} transaction${included.length === 1 ? '' : 's'}`}
          </button>
          <button
            type="button"
            className="btn"
            disabled={discard.isPending}
            onClick={() => discard.mutate()}
            data-testid="discard-import"
          >
            Discard this statement
          </button>
        </div>
      ) : null}
    </div>
  );
}
