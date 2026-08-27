'use client';

import { useMutation } from '@tanstack/react-query';
import { useState } from 'react';

import { Card, CardHeader, Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';

export function ExportCard() {
  const [error, setError] = useState<string | null>(null);

  const download = useMutation({
    mutationFn: api.exportData,
    onSuccess: ({ blob, filename }) => {
      setError(null);
      // The endpoint needs a bearer token, so the file arrives as a blob and
      // is handed to the browser through a temporary object URL.
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    },
    onError: (cause) =>
      setError(cause instanceof Error ? cause.message : 'The export could not be built.'),
  });

  return (
    <Card>
      <CardHeader title="Export your data" subtitle="Everything this account holds, as a ZIP" />
      <div className="p-5">
        <p className="text-sm leading-relaxed text-ink-muted">
          Transactions, receipts, alerts, corrections and saved analyses, as CSV and JSON.
          Amounts are written as decimals; internally Ledger AI stores integer cents and
          never uses floating point for money.
        </p>

        <button
          type="button"
          onClick={() => download.mutate()}
          disabled={download.isPending}
          className="btn-primary mt-4"
        >
          {download.isPending ? <Spinner /> : null}
          {download.isPending ? 'Building your export…' : 'Download export'}
        </button>

        {error ? (
          <p role="alert" className="mt-3 text-sm text-negative">
            {error}
          </p>
        ) : null}
      </div>
    </Card>
  );
}
