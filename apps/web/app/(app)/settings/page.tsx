'use client';

import { useQuery } from '@tanstack/react-query';

import { DangerZone } from '@/components/settings/danger-zone';
import { ExportCard } from '@/components/settings/export-card';
import { AiBadge, Badge, Card, CardHeader, ErrorState, Skeleton } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { queryKeys } from '@/lib/query-keys';

export default function SettingsPage() {
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: queryKeys.profile,
    queryFn: api.profile,
  });

  if (isLoading) {
    return (
      <div className="space-y-6">
        <Skeleton className="h-8 w-40" />
        <Skeleton className="h-48" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  if (isError || !data) {
    return (
      <Card>
        <ErrorState message={(error as Error)?.message ?? 'Could not load your profile.'} onRetry={() => void refetch()} />
      </Card>
    );
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Your profile, what Ledger AI is actually running, and what is not built yet.
        </p>
      </header>

      <Card>
        <CardHeader title="Profile" />
        <dl className="divide-y divide-line">
          {[
            ['Email', data.email],
            ['Display name', data.display_name],
            ['Account type', data.is_demo ? 'Demo account — synthetic data only' : 'Standard'],
            ['Transactions', data.transaction_count.toLocaleString()],
            ['Accounts', data.account_count.toLocaleString()],
            ['Uploads', data.upload_count.toLocaleString()],
          ].map(([label, value]) => (
            <div key={label} className="flex items-center justify-between gap-4 px-5 py-3">
              <dt className="text-sm text-ink-muted">{label}</dt>
              <dd className="text-sm font-medium text-ink">{value}</dd>
            </div>
          ))}
        </dl>
      </Card>

      <Card>
        <CardHeader
          title="AI disclosure"
          subtitle="What is producing your answers right now"
          action={<AiBadge aiEnabled={data.ai_enabled} />}
        />
        <div className="p-5">
          <p className="text-sm leading-relaxed text-ink">{data.ai_disclosure}</p>
          <ul className="mt-4 space-y-2 border-t border-line pt-4 text-xs leading-relaxed text-ink-muted">
            <li>
              <strong className="font-medium text-ink">Numbers are never model-written.</strong>{' '}
              Every figure is a SQL aggregate over your own transactions, and the explanation is
              checked against that result set before it is shown.
            </li>
            <li>
              <strong className="font-medium text-ink">Minimum data leaves the system.</strong>{' '}
              When AI is enabled, only merchant names and already-computed totals are sent — never
              raw uploaded files, account identifiers, or full statements.
            </li>
            <li>
              <strong className="font-medium text-ink">Not financial advice.</strong> Ledger AI
              reports on the data you upload and does not make recommendations.
            </li>
          </ul>
        </div>
      </Card>

      <Card>
        <CardHeader title="Features" subtitle="Honest status of every capability" />
        <ul className="divide-y divide-line">
          {data.features.map((feature) => (
            <li key={feature.key} className="flex items-start justify-between gap-4 px-5 py-3.5">
              <div className="min-w-0">
                <p className="text-sm font-medium text-ink">{feature.label}</p>
                <p className="mt-0.5 text-xs leading-relaxed text-ink-muted">{feature.note}</p>
              </div>
              <Badge tone={feature.available ? 'positive' : 'neutral'}>
                {feature.available ? 'Available' : 'Not yet'}
              </Badge>
            </li>
          ))}
        </ul>
      </Card>

      <Card>
        <CardHeader title="Connected accounts" subtitle="Future integration" />
        <div className="p-5">
          <div className="rounded-lg border border-dashed border-line bg-surface-sunken p-4">
            <Badge tone="neutral">Not implemented</Badge>
            <p className="mt-2 text-sm text-ink">
              Ledger AI does not connect to any real financial institution.
            </p>
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">
              This build works only with files you upload, and every figure in the demo account is
              synthetic. A real aggregator integration would require bank-grade credential handling
              and is deliberately out of scope for this project.
            </p>
          </div>
        </div>
      </Card>

      <ExportCard />

      <DangerZone />
    </div>
  );
}
