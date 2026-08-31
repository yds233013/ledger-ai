'use client';

import { useQuery } from '@tanstack/react-query';

import { ConsentForm } from '@/components/settings/consent-form';
import { Badge, Card, CardHeader, Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { CONSENT_COPY, CONSENT_ORDER } from '@/lib/consent';
import { queryKeys } from '@/lib/query-keys';

/**
 * The standing record of what this account has agreed to.
 *
 * Shows the accepted version next to each document, so somebody can see what
 * they agreed to rather than only that they agreed to something. Anything
 * outstanding — a first visit, or a document that has changed since — appears
 * as an unticked box here as well as in front of the upload page.
 */
export function ConsentCard({ isDemo }: { isDemo: boolean }) {
  // A demo account holds synthetic data and deletes itself within a day.
  // Putting a legal wall in front of a one-click demo would cost the demo and
  // protect nobody, so the card is absent and the request is never made.
  const { data, isLoading } = useQuery({
    queryKey: queryKeys.consents,
    queryFn: api.consents,
    enabled: !isDemo,
  });

  if (isDemo) return null;

  return (
    <Card>
      <CardHeader
        title="Agreements"
        subtitle="What you have accepted, and at which version"
        action={
          data && data.missing.length > 0 ? (
            <Badge tone="negative">{data.missing.length} outstanding</Badge>
          ) : data ? (
            <Badge tone="positive">Up to date</Badge>
          ) : null
        }
      />
      <div className="p-5">
        {isLoading || !data ? (
          <Spinner />
        ) : (
          <>
            <dl className="divide-y divide-line rounded-lg border border-line">
              {CONSENT_ORDER.map((type) => {
                const accepted = data.accepted[type];
                const current = accepted === data.required[type];
                return (
                  <div key={type} className="flex items-center justify-between gap-4 px-4 py-3">
                    <dt className="min-w-0">
                      <span className="block text-sm text-ink">{CONSENT_COPY[type].label}</span>
                      <span className="mt-0.5 block text-xs text-ink-faint">
                        Required: v{data.required[type]}
                      </span>
                    </dt>
                    <dd className="shrink-0 text-sm font-medium">
                      {current ? (
                        <span className="text-ink-muted">Accepted v{accepted}</span>
                      ) : accepted ? (
                        <span className="text-negative">v{accepted} — out of date</span>
                      ) : (
                        <span className="text-negative">Not accepted</span>
                      )}
                    </dd>
                  </div>
                );
              })}
            </dl>

            {data.missing.length > 0 ? (
              <div className="mt-5 border-t border-line pt-5">
                <p className="mb-4 text-sm text-ink-muted">
                  Uploading is unavailable until these are accepted. Everything else — reading,
                  exporting and deleting your own data — is never gated on them.
                </p>
                <ConsentForm state={data} submitLabel="Accept" />
              </div>
            ) : null}
          </>
        )}
      </div>
    </Card>
  );
}
