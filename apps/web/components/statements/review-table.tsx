'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { Badge } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { formatCents, formatDate } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';
import type { StatementRow } from '@/lib/types';

/**
 * What each parser flag means, in words rather than tokens.
 *
 * The flags exist to tell somebody which rows to look at, so they have to read
 * as instructions. "sign_unresolved" tells the reader nothing; "check whether
 * it is money in or out" tells them exactly what to do.
 */
const FLAG_COPY: Record<string, string> = {
  balance_mismatch:
    'The running balance does not move by this amount — check it against your statement.',
  sign_unresolved:
    'This amount was printed without a sign and there is no earlier balance to compare it to. Check whether it is money in or out.',
  sign_from_balance: 'Direction taken from the change in balance.',
  year_inferred: 'The year came from the statement period, not from this line.',
  no_balance_column: 'This statement has no balance column, so amounts could not be cross-checked.',
  short_description: 'Very little description text was found on this line.',
};

export function ReviewTable({
  importId,
  rows,
  editable,
}: {
  importId: string;
  rows: StatementRow[];
  editable: boolean;
}) {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState<string | null>(null);

  const update = useMutation({
    mutationFn: ({ rowId, body }: { rowId: string; body: Record<string, unknown> }) =>
      api.updateStatementRow(importId, rowId, body),
    onSettled: () => {
      setPending(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.statementImport(importId) });
    },
  });

  const toggle = (row: StatementRow) => {
    setPending(row.id);
    update.mutate({ rowId: row.id, body: { excluded: !row.excluded } });
  };

  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-line text-left text-xs uppercase tracking-wide text-ink-faint">
            <th className="px-4 py-2.5 font-medium">Import</th>
            <th className="px-4 py-2.5 font-medium">Date</th>
            <th className="px-4 py-2.5 font-medium">Description</th>
            <th className="px-4 py-2.5 font-medium">In / out</th>
            <th className="px-4 py-2.5 text-right font-medium">Amount</th>
            <th className="px-4 py-2.5 text-right font-medium">Balance</th>
            <th className="px-4 py-2.5 font-medium">Confidence</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {rows.map((row) => (
            <tr
              key={row.id}
              className={row.excluded ? 'opacity-45' : undefined}
              data-testid={`statement-row-${row.id}`}
            >
              <td className="px-4 py-3">
                <input
                  type="checkbox"
                  checked={!row.excluded}
                  disabled={!editable || pending === row.id}
                  onChange={() => toggle(row)}
                  aria-label={row.excluded ? 'Include this row' : 'Exclude this row'}
                  className="size-4 accent-brand"
                  data-testid={`include-${row.id}`}
                />
              </td>
              <td className="whitespace-nowrap px-4 py-3 text-ink-muted">
                {formatDate(row.posted_date)}
              </td>
              <td className="px-4 py-3">
                <span className="text-ink">{row.description}</span>
                {row.duplicate_of_existing ? (
                  <span className="ml-2 align-middle">
                    <Badge tone="neutral">Already imported</Badge>
                  </span>
                ) : null}
                {row.flags.length > 0 ? (
                  <span className="mt-1 block text-xs leading-relaxed text-ink-muted">
                    {row.flags.map((flag) => FLAG_COPY[flag] ?? flag).join(' ')}
                  </span>
                ) : null}
              </td>
              <td className="px-4 py-3">
                <Badge tone={row.direction === 'credit' ? 'positive' : 'neutral'}>
                  {row.direction === 'credit' ? 'Money in' : 'Money out'}
                </Badge>
              </td>
              <td className="whitespace-nowrap px-4 py-3 text-right font-medium tabular-nums text-ink">
                {formatCents(row.amount_cents)}
              </td>
              <td className="whitespace-nowrap px-4 py-3 text-right tabular-nums text-ink-muted">
                {row.balance_cents === null ? '—' : formatCents(row.balance_cents)}
              </td>
              <td className="px-4 py-3">
                {row.edited ? (
                  <Badge tone="positive">You corrected this</Badge>
                ) : row.confidence >= 0.8 ? (
                  <Badge tone="positive">High</Badge>
                ) : (
                  <Badge tone="negative">Check this</Badge>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
