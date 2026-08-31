'use client';

import { useQuery } from '@tanstack/react-query';

import { Card, CardHeader } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { formatBytes } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';

function Meter({
  label,
  used,
  limit,
  render,
}: {
  label: string;
  used: number;
  limit: number;
  render: (value: number) => string;
}) {
  const fraction = limit > 0 ? Math.min(1, used / limit) : 0;
  // The bar warns before the wall rather than at it: a limit you only learn
  // about by hitting it arrives as a failed upload. The palette has no amber,
  // so the near-limit state is a softened version of the same red.
  const tone = fraction >= 1 ? 'bg-negative' : fraction >= 0.8 ? 'bg-negative/60' : 'bg-brand';

  return (
    <div className="px-5 py-3.5">
      <div className="flex items-baseline justify-between gap-4">
        <span className="text-sm text-ink">{label}</span>
        <span className="font-mono text-xs text-ink-muted">
          {render(used)} / {render(limit)}
        </span>
      </div>
      <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-surface-sunken">
        <div
          className={`h-full rounded-full ${tone}`}
          style={{ width: `${Math.max(fraction * 100, used > 0 ? 2 : 0)}%` }}
        />
      </div>
    </div>
  );
}

const count = (value: number) => value.toLocaleString();

/**
 * How much of each private-beta budget this account has used.
 *
 * These are the limits of a beta running on one person's rented servers, not a
 * product decision about what anybody should be allowed to keep — which is why
 * the card says so rather than presenting them as a plan tier.
 */
export function UsageCard() {
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.usage,
    queryFn: api.usage,
  });

  // Nothing is rendered until the answer is known. Showing a card headed
  // "Usage" that then vanishes for a demo account is worse than a beat of
  // nothing, and the settings page already has its own loading state.
  if (isLoading || !data?.applies) return null;

  return (
    <Card>
      <CardHeader title="Usage" subtitle="Your private-beta limits" />
      <div className="divide-y divide-line">
        <Meter
          label="Uploads today"
          used={data.uploads_today}
          limit={data.uploads_per_day}
          render={count}
        />
        <Meter
          label="Data uploaded today"
          used={data.bytes_today}
          limit={data.upload_bytes_per_day}
          render={formatBytes}
        />
        <Meter
          label="Stored files"
          used={data.stored_bytes}
          limit={data.stored_bytes_limit}
          render={formatBytes}
        />
        <Meter
          label="Transactions"
          used={data.transaction_rows}
          limit={data.transaction_rows_limit}
          render={count}
        />
        <Meter label="Receipts" used={data.receipts} limit={data.receipts_limit} render={count} />
      </div>
      <p className="border-t border-line px-5 py-3 text-xs leading-relaxed text-ink-muted">
        Daily counts reset at midnight UTC
        {/* Rendered in the reader's own zone and labelled as such. "midnight UTC
            (5:00:00 PM)" reads as a contradiction rather than a translation. */}
        {data.resets_at
          ? `, which is ${new Date(data.resets_at).toLocaleString()} where you are`
          : ''}
        . Up to{' '}
        {data.concurrent_jobs_limit} files process at once, and a single file can be up to{' '}
        {formatBytes(data.max_upload_bytes)}. Deleting files frees stored space; it does not return
        a day&rsquo;s upload count.
      </p>
      <p className="border-t border-line px-5 py-3 text-xs leading-relaxed text-ink-faint">
        These are the limits of a private beta running on rented servers, not a judgement about how
        much anybody should be able to keep. They will change.
      </p>
    </Card>
  );
}
