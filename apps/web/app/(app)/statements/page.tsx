'use client';

import { useQuery } from '@tanstack/react-query';
import Link from 'next/link';

import { Badge, Card, CardHeader, EmptyState, Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { formatDate } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';

export default function StatementsPage() {
  const { data = [], isLoading } = useQuery({
    queryKey: queryKeys.statementImports,
    queryFn: api.statementImports,
  });

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Statements</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Statement PDFs you have uploaded. Nothing from one reaches your transactions until you
          review and import it.
        </p>
      </header>

      <Card>
        <CardHeader
          title="Statement imports"
          action={
            <Link href="/upload" className="text-xs font-medium text-brand hover:underline">
              Upload a statement
            </Link>
          }
        />
        {isLoading ? (
          <div className="p-5">
            <Spinner />
          </div>
        ) : data.length === 0 ? (
          <EmptyState
            title="No statement imports"
            description="Upload a bank statement PDF and the transactions read from it will appear here for review."
          />
        ) : (
          <ul className="divide-y divide-line">
            {data.map((item) => (
              <li key={item.id} className="px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="text-sm font-medium text-ink">
                      {item.period_start && item.period_end
                        ? `${formatDate(item.period_start)} to ${formatDate(item.period_end)}`
                        : 'Statement'}
                    </p>
                    <p className="mt-0.5 text-xs text-ink-muted">
                      {item.row_count} row(s) · {item.page_count} page(s)
                      {item.status !== 'committed'
                        ? ` · expires ${formatDate(item.expires_at)}`
                        : ''}
                    </p>
                  </div>
                  <div className="flex items-center gap-3">
                    <Badge tone={item.status === 'committed' ? 'positive' : 'brand'}>
                      {item.status === 'committed' ? 'Imported' : 'Needs review'}
                    </Badge>
                    <Link
                      href={`/statements/${item.id}`}
                      className="text-xs font-medium text-brand hover:underline"
                    >
                      {item.status === 'committed' ? 'View' : 'Review'}
                    </Link>
                  </div>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
