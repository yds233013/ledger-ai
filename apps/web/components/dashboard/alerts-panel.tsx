'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useState } from 'react';

import { Badge, Card, CardHeader, EmptyState, Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { formatShortDate, formatSpend } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';
import type { Alert, AlertSeverity } from '@/lib/types';

const TYPE_LABEL: Record<Alert['alert_type'], string> = {
  duplicate: 'Possible duplicate',
  near_duplicate: 'Charged twice?',
  unusual_amount: 'Unusually large',
  new_merchant: 'First time here',
  large_for_merchant: 'Large for this merchant',
};

/**
 * How each severity is presented.
 *
 * Duplicates are the only class that suggests an action, so they lead and are
 * the only ones styled as something to review. Everything else is an
 * observation about the user's own spending — and none of it is a fraud claim.
 */
const PRIORITY: Record<
  AlertSeverity,
  { heading: string; blurb: string; tone: 'negative' | 'caution' | 'neutral'; rail: string }
> = {
  high: {
    heading: 'Worth reviewing',
    blurb: 'These look like the same charge appearing twice.',
    tone: 'negative',
    rail: 'border-l-negative',
  },
  medium: {
    heading: 'Unusual for you',
    blurb: 'Larger than your own history would suggest. Often perfectly normal.',
    tone: 'caution',
    rail: 'border-l-caution',
  },
  low: {
    heading: 'For information',
    blurb: 'Noted in passing — nothing to act on.',
    tone: 'neutral',
    rail: 'border-l-line',
  },
};

const ORDER: AlertSeverity[] = ['high', 'medium', 'low'];

/** Render the evidence dict so an alert can be audited, not just believed. */
function Evidence({ evidence }: { evidence: Record<string, unknown> }) {
  const shown = Object.entries(evidence).filter(
    ([key]) => key !== 'disclaimer' && key !== 'matched_transaction_ids',
  );
  return (
    <dl className="mt-2 space-y-1">
      {shown.map(([key, value]) => (
        <div key={key} className="grid grid-cols-[132px_1fr] gap-2">
          <dt className="text-xs text-ink-faint">{key.replace(/_/g, ' ')}</dt>
          <dd className="text-xs leading-relaxed text-ink-muted">{String(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

function AlertRow({
  alert,
  onDismiss,
  isActing,
}: {
  alert: Alert;
  onDismiss: () => void;
  isActing: boolean;
}) {
  const priority = PRIORITY[alert.severity];

  return (
    <li className={`border-l-2 ${priority.rail} px-5 py-4`}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <Badge tone={priority.tone}>{TYPE_LABEL[alert.alert_type] ?? alert.alert_type}</Badge>
        <span className="text-xs tabular-nums text-ink-muted">
          {formatShortDate(alert.transaction_date)} ·{' '}
          {formatSpend(Math.round(alert.transaction_amount * 100))}
        </span>
      </div>

      <p className="mt-2 text-sm leading-relaxed text-ink">{alert.message}</p>

      {alert.severity_note ? (
        <p className="mt-1 text-xs text-ink-faint">{alert.severity_note}</p>
      ) : null}

      <details className="mt-2">
        <summary className="cursor-pointer text-xs text-brand hover:underline">
          Why this was flagged
        </summary>
        <Evidence evidence={alert.evidence} />
      </details>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <Link
          href={`/transactions?search=${encodeURIComponent(alert.transaction_merchant)}`}
          className="btn-secondary"
        >
          Review transaction
        </Link>
        <button type="button" onClick={onDismiss} disabled={isActing} className="btn-ghost">
          {isActing ? <Spinner className="h-3 w-3" /> : null}
          Dismiss
        </button>
      </div>
    </li>
  );
}

export function AlertsPanel({
  alerts,
  openCount,
  note,
}: {
  alerts: Alert[];
  openCount: number;
  note: string;
}) {
  // The panel deliberately shows only the most serious alerts. Saying so is
  // the difference between a considered summary and a list that looks complete
  // but isn't.
  const hidden = Math.max(0, openCount - alerts.length);
  const queryClient = useQueryClient();
  const [actingId, setActingId] = useState<string | null>(null);

  const act = useMutation({
    mutationFn: ({ id, status }: { id: string; status: 'dismissed' | 'resolved' }) =>
      api.updateAlert(id, status),
    onMutate: ({ id }) => setActingId(id),
    onSettled: () => {
      setActingId(null);
      void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
      void queryClient.invalidateQueries({ queryKey: queryKeys.alertsAll });
    },
  });

  // Grouped by priority so the one class worth acting on is never buried
  // under a list of informational notes.
  const grouped = ORDER.map((severity) => ({
    severity,
    items: alerts.filter((alert) => alert.severity === severity),
  })).filter((group) => group.items.length > 0);

  return (
    <Card>
      <CardHeader
        title="Alerts"
        subtitle={openCount > 0 ? `${openCount} open` : 'Nothing unusual right now'}
        action={openCount > 0 ? <Badge tone="caution">{openCount}</Badge> : null}
      />

      {alerts.length === 0 ? (
        <EmptyState
          title="No open alerts"
          description="Ledger AI watches for duplicate charges and amounts that are unusual for you. Nothing stands out at the moment."
        />
      ) : (
        grouped.map((group) => (
          <section key={group.severity} aria-label={PRIORITY[group.severity].heading}>
            <div className="flex items-baseline justify-between gap-3 border-y border-line bg-surface-sunken/60 px-5 py-2">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-ink">
                {PRIORITY[group.severity].heading}
              </h3>
              <span className="text-xs tabular-nums text-ink-faint">
                {group.items.length}
              </span>
            </div>
            <p className="px-5 pt-2 text-xs leading-relaxed text-ink-muted">
              {PRIORITY[group.severity].blurb}
            </p>
            <ul className="divide-y divide-line">
              {group.items.map((alert) => (
                <AlertRow
                  key={alert.id}
                  alert={alert}
                  isActing={actingId === alert.id}
                  onDismiss={() => act.mutate({ id: alert.id, status: 'dismissed' })}
                />
              ))}
            </ul>
          </section>
        ))
      )}

      {openCount > 0 ? (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-5 py-3">
          <p className="text-xs text-ink-muted">
            {hidden > 0
              ? `Showing ${alerts.length} of ${openCount} alerts, most serious first.`
              : `Showing all ${openCount} open alerts, most serious first.`}
          </p>
          {/*
            ?flagged=1, NOT ?review=needs_review.
            `needs_review` is the low-confidence categorization queue, which is
            a different fact about a transaction: a duplicate charge at a
            known merchant is categorized at confidence 1.00 and never enters
            it. Linking there showed people transactions unrelated to the
            alerts they had just been reading about.
          */}
          <Link
            href="/transactions?flagged=1"
            className="text-xs font-medium text-brand hover:underline"
          >
            View all flagged transactions
          </Link>
        </div>
      ) : null}

      <p className="border-t border-line px-5 py-3 text-xs leading-relaxed text-ink-faint">
        {note}
      </p>
    </Card>
  );
}
