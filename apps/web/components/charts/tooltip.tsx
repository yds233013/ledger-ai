'use client';

import { formatMoney } from '@/lib/format';

interface TooltipEntry {
  name?: string;
  value?: number | string;
  payload?: { label?: string; count?: number; value?: number };
}

/**
 * Shared hover layer. Every chart in the app ships one — an SVG chart that
 * cannot be interrogated on hover is a picture, not a visualization.
 */
export function ChartTooltip({
  active,
  payload,
  label,
  valueFormat = 'currency',
}: {
  active?: boolean;
  payload?: TooltipEntry[];
  label?: string;
  valueFormat?: 'currency' | 'number';
}) {
  if (!active || !payload?.length) return null;

  const entry = payload[0];
  const value = Number(entry?.value ?? 0);
  const count = entry?.payload?.count;

  return (
    <div className="rounded-lg border border-line bg-surface-raised px-3 py-2 shadow-lg">
      <p className="text-xs font-medium text-ink">{label ?? entry?.payload?.label}</p>
      <p className="mt-0.5 text-sm font-semibold text-ink">
        {valueFormat === 'currency' ? formatMoney(value) : value.toLocaleString()}
      </p>
      {typeof count === 'number' ? (
        <p className="mt-0.5 text-xs text-ink-muted">
          {count} transaction{count === 1 ? '' : 's'}
        </p>
      ) : null}
    </div>
  );
}
