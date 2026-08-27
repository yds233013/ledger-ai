'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';

import { CategoryBarChart, TrendLineChart } from '@/components/charts/spend-charts';
import { AlertsPanel } from '@/components/dashboard/alerts-panel';
import { StatTile } from '@/components/dashboard/stat-tile';
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Skeleton,
} from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { formatCents, formatDate, formatShortDate, formatSpend } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';

export default function DashboardPage() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: queryKeys.dashboard,
    queryFn: api.dashboard,
  });

  if (isLoading) return <DashboardSkeleton />;

  if (isError) {
    return (
      <Card>
        <ErrorState message={(error as Error).message} onRetry={() => void refetch()} />
      </Card>
    );
  }

  if (!data || data.transaction_count === 0) {
    return (
      <Card>
        <EmptyState
          title="No transactions yet"
          description="Upload a CSV bank statement to see your spending broken down here."
          action={
            <Link href="/upload" className="btn-primary">
              Upload a statement
            </Link>
          }
        />
      </Card>
    );
  }

  const categoryData = data.by_category.map((slice) => ({
    label: slice.label,
    value: slice.value,
    count: slice.transaction_count,
  }));
  const trendData = data.trend.map((point) => ({ label: point.label, value: point.value }));

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Dashboard</h1>
          <p className="mt-1 text-sm text-ink-muted">
            {data.period_label} · {data.account_count} synthetic accounts ·{' '}
            {data.earliest_transaction ? (
              <>
                data from {formatDate(data.earliest_transaction)}
                {data.latest_transaction ? ` to ${formatDate(data.latest_transaction)}` : null}
              </>
            ) : null}
          </p>
        </div>
        <Link href="/ask" className="btn-secondary">
          Ask a question
        </Link>
      </header>

      {data.currency_note ? (
        <p className="rounded-lg border border-line bg-surface-sunken px-4 py-3 text-xs leading-relaxed text-ink-muted">
          <strong className="font-medium text-ink">Currency.</strong> {data.currency_note}
        </p>
      ) : null}

      <section
        aria-label="Key figures"
        className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4"
      >
        <StatTile
          label={`Spending — ${data.period_label} (${data.base_currency})`}
          value={formatCents(data.total_spend_cents)}
          delta={data.delta_pct}
          deltaDirection={data.delta_direction}
          emphasis
        />
        <StatTile
          label="Previous month"
          value={formatCents(data.previous_spend_cents)}
          hint={`${data.transaction_count} transactions this period`}
        />
        <StatTile
          label="Income this period"
          value={formatCents(data.total_income_cents)}
          hint="Deposits and payroll"
        />
        <StatTile
          label="Needs review"
          value={(data.needs_review_count + data.pending_receipt_count).toLocaleString()}
          hint={
            data.pending_receipt_count > 0
              ? `${data.needs_review_count} transactions and ${data.pending_receipt_count} receipts`
              : data.needs_review_count > 0
                ? 'Low-confidence categories to confirm'
                : 'Everything categorized confidently'
          }
        />
      </section>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Spending by category"
            subtitle={`${data.period_label} · transfers and card payments excluded`}
          />
          <div className="p-4">
            {categoryData.length ? (
              <CategoryBarChart data={categoryData} />
            ) : (
              <EmptyState
                title="No spending this period"
                description="There are no outflows in the selected month."
              />
            )}
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Spending trend"
            subtitle={
              // Say what is actually plotted. A young account has fewer than
              // twelve months of history, and claiming twelve while drawing a
              // flat line along the axis reads as "spent nothing", not "no data".
              data.trend_months >= 12
                ? 'Last 12 months'
                : `Last ${data.trend_months} month${data.trend_months === 1 ? '' : 's'}`
            }
          />
          <div className="p-4">
            <TrendLineChart data={trendData} />
          </div>
        </Card>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader
            title="Recent transactions"
            action={
              <Link href="/transactions" className="text-xs font-medium text-brand hover:underline">
                View all
              </Link>
            }
          />
          <ul className="divide-y divide-line">
            {data.recent.map((transaction) => (
              <li key={transaction.id} className="flex items-center gap-3 px-5 py-3">
                <span
                  aria-hidden="true"
                  className="h-2 w-2 shrink-0 rounded-full"
                  style={{ backgroundColor: transaction.color }}
                />
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-ink">{transaction.merchant}</p>
                  <p className="text-xs text-ink-muted">
                    {formatShortDate(transaction.posted_date)} · {transaction.category}
                  </p>
                </div>
                {transaction.needs_review ? <Badge tone="caution">Review</Badge> : null}
                <span
                  className={`shrink-0 text-sm font-medium tabular-nums ${
                    transaction.amount_cents > 0 ? 'text-positive' : 'text-ink'
                  }`}
                >
                  {transaction.amount_cents > 0 ? '+' : '−'}
                  {formatSpend(transaction.amount_cents)}
                </span>
              </li>
            ))}
          </ul>
        </Card>

        <AlertsPanel
          alerts={data.alerts}
          openCount={data.open_alert_count}
          note={data.alerts_note}
        />
      </div>
    </div>
  );
}

function DashboardSkeleton() {
  return (
    <div className="space-y-6">
      <Skeleton className="h-8 w-48" />
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {Array.from({ length: 4 }).map((_, index) => (
          <Skeleton key={index} className="h-28" />
        ))}
      </div>
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        <Skeleton className="h-80" />
        <Skeleton className="h-80" />
      </div>
    </div>
  );
}
