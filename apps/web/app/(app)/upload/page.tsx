'use client';

import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import Link from 'next/link';
import { useCallback, useState } from 'react';

import { ConsentGate } from '@/components/upload/consent-gate';
import { Dropzone } from '@/components/upload/dropzone';
import { KindPicker } from '@/components/upload/kind-picker';
import { JobProgress } from '@/components/upload/job-progress';
import { Badge, Card, CardHeader, EmptyState, Spinner } from '@/components/ui/primitives';
import { ApiError, api } from '@/lib/api-client';
import { formatBytes, formatDate } from '@/lib/format';
import { queryKeys } from '@/lib/query-keys';
import type { Upload } from '@/lib/types';

/** Poll only while a job is genuinely in flight. */
const ACTIVE_STAGES = new Set(['queued', 'extracting', 'normalizing', 'categorizing']);

function isActive(upload: Upload): boolean {
  return upload.job ? ACTIVE_STAGES.has(upload.job.stage) : false;
}

export default function UploadPage() {
  const queryClient = useQueryClient();
  const [notice, setNotice] = useState<{ tone: 'info' | 'error'; text: string } | null>(null);
  // Only consulted for PDFs. The server refuses a PDF that does not declare
  // which it is, so this is the answer rather than a hint.
  const [pdfKind, setPdfKind] = useState<'statement' | 'receipt'>('statement');

  const { data: uploads = [], isLoading } = useQuery({
    queryKey: queryKeys.uploads,
    queryFn: api.uploads,
    // While anything is processing, poll briskly so the stage list actually
    // moves; once everything settles, stop polling entirely.
    refetchInterval: (query) => {
      const current = query.state.data as Upload[] | undefined;
      return current?.some(isActive) ? 700 : false;
    },
    // Without this, TanStack pauses the interval whenever the tab loses focus.
    // A job finishes in seconds, so switching tabs mid-upload would otherwise
    // leave the progress frozen at "queued" indefinitely.
    refetchIntervalInBackground: true,
    // And this resyncs the moment the user comes back, since refetch-on-focus
    // is disabled globally.
    refetchOnWindowFocus: true,
  });

  const upload = useMutation({
    mutationFn: (file: File) =>
      api.createUpload(file, file.type === 'application/pdf' ? pdfKind : undefined),
    onSuccess: (result) => {
      if (result.duplicate_of_existing) {
        setNotice({
          tone: 'info',
          text:
            result.message ??
            'That exact file was already processed, so no duplicate transactions were created.',
        });
      } else if (result.kind === 'statement_pdf') {
        setNotice({
          tone: 'info',
          text: `${result.original_filename} is being read. It will appear below for review — nothing is imported until you confirm it.`,
        });
      } else {
        setNotice({ tone: 'info', text: `${result.original_filename} queued for processing.` });
      }
      void queryClient.invalidateQueries({ queryKey: queryKeys.uploads });
    },
    onError: (error) => {
      setNotice({
        tone: 'error',
        text: error instanceof ApiError ? error.message : 'Upload failed. Please try again.',
      });
    },
  });

  const handleFiles = useCallback(
    (files: File[]) => {
      setNotice(null);
      for (const file of files) upload.mutate(file);
    },
    [upload],
  );

  // When a job finishes, the rest of the app is stale.
  const settled = uploads.filter((item) => item.job?.stage === 'complete').length;
  const [lastSettled, setLastSettled] = useState(settled);
  if (settled !== lastSettled) {
    setLastSettled(settled);
    void queryClient.invalidateQueries({ queryKey: queryKeys.dashboard });
    void queryClient.invalidateQueries({ queryKey: queryKeys.transactionsAll });
    void queryClient.invalidateQueries({ queryKey: queryKeys.facets });
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Upload</h1>
        <p className="mt-1 text-sm text-ink-muted">
          Import a CSV bank statement, or a receipt to read with OCR. Uploading the same
          file twice never creates duplicate transactions.
        </p>
      </header>

      <ConsentGate>
        <Card className="p-5">
          <Dropzone onFiles={handleFiles} disabled={upload.isPending} />

          <KindPicker value={pdfKind} onChange={setPdfKind} disabled={upload.isPending} />

          {upload.isPending ? (
            <p className="mt-3 flex items-center gap-2 text-sm text-ink-muted">
              <Spinner /> Uploading…
            </p>
          ) : null}

          {notice ? (
            <p
              role="status"
              className={`mt-3 rounded-lg border px-3 py-2 text-sm ${
                notice.tone === 'error'
                  ? 'border-negative/30 bg-negative/5 text-negative'
                  : 'border-line bg-surface-sunken text-ink-muted'
              }`}
            >
              {notice.text}
            </p>
          ) : null}

          <div className="mt-5 rounded-lg border border-line bg-surface-sunken p-4">
            <p className="text-xs font-medium text-ink">Expected CSV columns</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">
              A date column (<code className="font-mono">Date</code>,{' '}
              <code className="font-mono">Posted Date</code>…), a description column (
              <code className="font-mono">Description</code>,{' '}
              <code className="font-mono">Memo</code>…), and either a signed{' '}
              <code className="font-mono">Amount</code> column or separate{' '}
              <code className="font-mono">Debit</code>/<code className="font-mono">Credit</code>{' '}
              columns. Column names are matched case-insensitively.
            </p>
            <p className="mt-2 text-xs text-ink-faint">
              A synthetic sample file lives at{' '}
              <code className="font-mono">docs/samples/sample_statement_synthetic.csv</code> in the
              repository.
            </p>
          </div>
          <div className="mt-4 rounded-lg border border-line bg-surface-sunken p-4">
            <p className="text-xs font-medium text-ink">Remove account numbers first</p>
            <p className="mt-1 text-xs leading-relaxed text-ink-muted">
              Ledger AI does not need a full account, card or Social Security number, and
              refuses files that contain one. Masked values are expected and fine — a column of{' '}
              <code className="font-mono">••••4821</code> is exactly what a bank export looks
              like. The check is best-effort and will not catch everything, so the safest thing
              is to look before you upload.
            </p>
          </div>
        </Card>
      </ConsentGate>

      <Card>
        <CardHeader
          title="Upload history"
          action={
            <span className="flex gap-3">
              <Link href="/receipts" className="text-xs font-medium text-brand hover:underline">
                Review receipts
              </Link>
              <Link href="/transactions" className="text-xs font-medium text-brand hover:underline">
                View transactions
              </Link>
            </span>
          }
        />

        {isLoading ? (
          <div className="p-5">
            <Spinner />
          </div>
        ) : uploads.length === 0 ? (
          <EmptyState
            title="No uploads yet"
            description="Files you import will appear here with their processing status."
          />
        ) : (
          <ul className="divide-y divide-line">
            {uploads.map((item) => (
              <li key={item.id} className="px-5 py-4">
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium text-ink">
                      {item.original_filename}
                    </p>
                    <p className="mt-0.5 text-xs text-ink-muted">
                      {formatBytes(item.size_bytes)} · {item.kind.toUpperCase()} ·{' '}
                      {formatDate(item.created_at)}
                    </p>
                  </div>
                  <Badge
                    tone={
                      item.job?.stage === 'complete'
                        ? 'positive'
                        : item.job?.stage === 'failed'
                          ? 'negative'
                          : 'brand'
                    }
                  >
                    {item.job?.stage ?? item.status}
                  </Badge>
                </div>

                {item.job ? (
                  <div className="mt-4 max-w-md">
                    <JobProgress job={item.job} />
                  </div>
                ) : null}

                {item.kind === 'statement_pdf' && item.job?.stage === 'complete' ? (
                  <Link
                    href="/statements"
                    className="mt-3 inline-block text-xs font-medium text-brand hover:underline"
                  >
                    Review the transactions read from this statement →
                  </Link>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}
