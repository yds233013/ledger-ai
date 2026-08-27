'use client';

import { Badge, Card, Spinner } from '@/components/ui/primitives';
import { formatShortDate, formatSpend } from '@/lib/format';
import type { MatchCandidate } from '@/lib/types';

/**
 * Possible existing transactions for this receipt.
 *
 * Nothing is ever linked automatically: the user picks a candidate and
 * confirms. Each suggestion shows the account, date, merchant, amount and
 * source upload so two similar charges can be told apart, plus why it was
 * suggested at all.
 */
export function MatchCandidates({
  candidates,
  isLoading,
  note,
  onLink,
  onReject,
  linkingId,
}: {
  candidates: MatchCandidate[];
  isLoading: boolean;
  note: string;
  onLink: (transactionId: string) => void;
  onReject: (transactionId: string) => void;
  linkingId: string | null;
}) {
  if (isLoading) {
    return (
      <Card className="p-4">
        <p className="flex items-center gap-2 text-sm text-ink-muted">
          <Spinner /> Looking for a matching transaction…
        </p>
      </Card>
    );
  }

  if (candidates.length === 0) {
    return (
      <Card className="p-4">
        <p className="text-sm font-medium text-ink">No matching transaction found</p>
        <p className="mt-1 text-xs leading-relaxed text-ink-muted">
          Nothing already imported looks like this receipt, so confirming it will create a
          new transaction.
        </p>
      </Card>
    );
  }

  return (
    <Card>
      <div className="border-b border-line px-4 py-3">
        <p className="text-sm font-medium text-ink">
          This receipt may already be imported
        </p>
        <p className="mt-1 text-xs leading-relaxed text-ink-muted">{note}</p>
      </div>

      <ul className="divide-y divide-line">
        {candidates.map((candidate) => (
          <li key={candidate.transaction_id} className="px-4 py-3">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="text-sm font-medium text-ink">{candidate.merchant}</p>
                <p className="mt-0.5 text-xs text-ink-muted">
                  {formatShortDate(candidate.posted_date)} ·{' '}
                  {formatSpend(candidate.amount_cents)} {candidate.currency} ·{' '}
                  {candidate.account_name}
                </p>
                <p className="mt-0.5 text-xs text-ink-faint">
                  {candidate.category ? `${candidate.category} · ` : ''}
                  {candidate.source_filename
                    ? `imported from ${candidate.source_filename}`
                    : 'entered without an upload'}
                </p>
              </div>
              <Badge tone="brand">match {Math.round(candidate.score * 100)}%</Badge>
            </div>

            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-brand hover:underline">
                Why this was suggested
              </summary>
              <ul className="mt-2 space-y-1">
                {candidate.signals.map((signal) => (
                  <li key={signal.name} className="text-xs text-ink-muted">
                    <span className="font-medium text-ink">{signal.name}</span> ·{' '}
                    {signal.detail}
                  </li>
                ))}
              </ul>
            </details>

            <div className="mt-3 flex flex-wrap gap-2">
              <button
                type="button"
                onClick={() => onLink(candidate.transaction_id)}
                disabled={linkingId !== null}
                className="btn-primary"
              >
                {linkingId === candidate.transaction_id ? <Spinner /> : null}
                Link to this transaction
              </button>
              <button
                type="button"
                onClick={() => onReject(candidate.transaction_id)}
                disabled={linkingId !== null}
                className="btn-ghost"
              >
                Not this one
              </button>
            </div>

            <p className="mt-2 text-xs text-ink-faint">
              Linking records the connection only. It does not change this
              transaction&apos;s merchant, category or amount.
            </p>
          </li>
        ))}
      </ul>
    </Card>
  );
}
