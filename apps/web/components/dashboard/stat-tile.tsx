import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';

/**
 * A single headline figure. Per the form heuristic, one number with no
 * comparison over time is a stat tile, not a chart.
 */
export function StatTile({
  label,
  value,
  delta,
  deltaDirection,
  hint,
  emphasis = false,
}: {
  label: string;
  value: ReactNode;
  delta?: number | null;
  deltaDirection?: 'up' | 'down' | 'flat';
  hint?: string;
  emphasis?: boolean;
}) {
  // For spending, up is not good news — the tone follows the meaning, and the
  // arrow plus the sign carry it too, so colour is never the only signal.
  const tone =
    deltaDirection === 'up'
      ? 'text-negative'
      : deltaDirection === 'down'
        ? 'text-positive'
        : 'text-ink-muted';

  return (
    <div className={cn('card p-5', emphasis && 'ring-1 ring-brand/20')}>
      <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">{label}</p>
      <p className="mt-2 text-2xl font-semibold tabular-nums tracking-tight text-ink">{value}</p>

      {delta !== undefined && deltaDirection ? (
        <p className={cn('mt-1.5 flex items-center gap-1 text-xs font-medium', tone)}>
          <span aria-hidden="true">
            {deltaDirection === 'up' ? '▲' : deltaDirection === 'down' ? '▼' : '■'}
          </span>
          <span>
            {delta === null || delta === undefined ? '—' : `${Math.abs(delta).toFixed(1)}%`}{' '}
            <span className="font-normal text-ink-muted">vs previous month</span>
          </span>
        </p>
      ) : hint ? (
        <p className="mt-1.5 text-xs text-ink-muted">{hint}</p>
      ) : null}
    </div>
  );
}
