'use client';

import { useQuery } from '@tanstack/react-query';

import { ConsentForm } from '@/components/settings/consent-form';
import { Card, CardHeader, Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { queryKeys } from '@/lib/query-keys';

/**
 * Stands in front of the upload form until the current documents are accepted.
 *
 * Upload is the one action gated this way, because it is the moment new
 * financial data enters the system. Reading, exporting and deleting your own
 * data are never gated — withholding somebody's own records until they accept a
 * new document would be leverage, not consent.
 *
 * This is a courtesy, not the enforcement. The API refuses the upload
 * regardless of what this component renders.
 */
export function ConsentGate({ children }: { children: React.ReactNode }) {
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.consents,
    queryFn: api.consents,
  });

  if (isLoading) {
    return (
      <Card className="p-5">
        <Spinner />
      </Card>
    );
  }

  // A failed lookup must not lock somebody out of their own upload page. The
  // API is the gate; if it disagrees it will say so, with a message that
  // explains what to accept.
  if (!data || data.missing.length === 0) return <>{children}</>;

  const first = Object.keys(data.accepted).length === 0;

  return (
    <div data-testid="consent-gate">
      <Card>
        <CardHeader
          title={first ? 'Before your first upload' : 'These documents have changed'}
          subtitle={
            first
              ? 'Three short documents, one read'
              : 'Please review what changed and accept again'
          }
        />
        <div className="p-5">
          <p className="mb-5 text-sm leading-relaxed text-ink-muted">
            {first
              ? 'Uploading is the point at which your financial data enters Ledger AI, so it is the one thing that asks first. Everything else — reading, exporting and deleting your own data — never will.'
              : 'You have accepted an earlier version of the documents below. Rather than assume that answer still stands, this asks again.'}
          </p>
          <ConsentForm state={data} submitLabel="Accept and start uploading" />
        </div>
      </Card>
    </div>
  );
}
