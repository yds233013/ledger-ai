'use client';

import { AnalysisChart } from '@/components/charts/spend-charts';
import { AiBadge, Badge, Card } from '@/components/ui/primitives';
import { formatDuration, formatMoney, formatShortDate, formatSpend } from '@/lib/format';
import type { AnalysisResult } from '@/lib/types';

export function Answer({
  result,
  aiEnabled,
}: {
  result: AnalysisResult;
  aiEnabled: boolean;
}) {
  if (result.declined) {
    return (
      <Card className="p-5">
        <Badge tone="caution">Out of scope</Badge>
        <p className="mt-3 text-sm leading-relaxed text-ink">{result.narration}</p>
      </Card>
    );
  }

  const data = result.result;
  const comparison = data?.comparison;

  return (
    <div className="space-y-4">
      <Card className="p-5">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <AiBadge aiEnabled={aiEnabled} />
          {result.cached ? <Badge tone="neutral">Cached result</Badge> : null}
          {typeof result.duration_ms === 'number' ? (
            <Badge tone="neutral">{formatDuration(result.duration_ms)}</Badge>
          ) : null}
        </div>

        {/* The headline figure. One number, so a stat tile rather than a chart. */}
        {data ? (
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <p className="text-3xl font-semibold tabular-nums tracking-tight text-ink">
              {data.metric_label === 'Count'
                ? data.total_cents.toLocaleString()
                : formatMoney(data.total)}
            </p>
            {comparison ? (
              <p
                className={`text-sm font-medium ${
                  comparison.direction === 'up' ? 'text-negative' : 'text-positive'
                }`}
              >
                <span aria-hidden="true">
                  {comparison.direction === 'up' ? '▲' : comparison.direction === 'down' ? '▼' : '■'}
                </span>{' '}
                {formatMoney(Math.abs(comparison.delta))}
                {comparison.delta_pct !== null
                  ? ` (${Math.abs(comparison.delta_pct).toFixed(1)}%)`
                  : ''}{' '}
                <span className="font-normal text-ink-muted">
                  vs {comparison.previous_label}
                </span>
              </p>
            ) : null}
          </div>
        ) : null}

        <p className="mt-3 text-sm leading-relaxed text-ink">{result.narration}</p>

        {result.caveats?.length ? (
          <ul className="mt-3 space-y-1 border-t border-line pt-3">
            {result.caveats.map((caveat) => (
              <li key={caveat} className="text-xs leading-relaxed text-ink-muted">
                · {caveat}
              </li>
            ))}
          </ul>
        ) : null}
      </Card>

      {result.chart && result.chart.kind !== 'none' ? (
        <Card className="p-5">
          <AnalysisChart spec={result.chart} />
        </Card>
      ) : null}

      {result.supporting_transactions.length ? (
        <Card>
          <details open>
            <summary className="cursor-pointer border-b border-line px-5 py-3 text-sm font-medium text-ink">
              Supporting transactions ({result.supporting_transactions.length})
            </summary>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] text-sm">
                <caption className="sr-only">
                  The individual transactions behind this answer
                </caption>
                <thead>
                  <tr className="border-b border-line bg-surface-sunken/60 text-left">
                    <th scope="col" className="px-4 py-2 text-xs font-medium text-ink-muted">
                      Date
                    </th>
                    <th scope="col" className="px-4 py-2 text-xs font-medium text-ink-muted">
                      Merchant
                    </th>
                    <th scope="col" className="px-4 py-2 text-xs font-medium text-ink-muted">
                      Category
                    </th>
                    <th scope="col" className="px-4 py-2 text-right text-xs font-medium text-ink-muted">
                      Amount
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {result.supporting_transactions.map((transaction) => (
                    <tr key={transaction.id} className="border-b border-line last:border-0">
                      <td className="whitespace-nowrap px-4 py-2 text-xs text-ink-muted tabular-nums">
                        {formatShortDate(transaction.posted_date)}
                      </td>
                      <td className="px-4 py-2 text-xs text-ink">{transaction.merchant}</td>
                      <td className="px-4 py-2">
                        <span className="inline-flex items-center gap-1.5 text-xs text-ink-muted">
                          <span
                            aria-hidden="true"
                            className="h-1.5 w-1.5 rounded-full"
                            style={{ backgroundColor: transaction.color }}
                          />
                          {transaction.category}
                        </span>
                      </td>
                      <td className="whitespace-nowrap px-4 py-2 text-right text-xs font-medium tabular-nums text-ink">
                        {formatSpend(transaction.amount_cents)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </Card>
      ) : null}
    </div>
  );
}
