'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { signOut } from 'next-auth/react';
import { useState } from 'react';

import { Badge, Card, CardHeader, Spinner } from '@/components/ui/primitives';
import { api, clearTokenCache } from '@/lib/api-client';
import { cn } from '@/lib/cn';
import type { DeletionResult } from '@/lib/types';

type Mode = 'data' | 'account';

const COPY: Record<Mode, { title: string; button: string; warning: string }> = {
  data: {
    title: 'Delete my data',
    button: 'Delete all my data',
    warning:
      'Removes every transaction, upload, receipt, alert and saved analysis. Your account and sign-in stay.',
  },
  account: {
    title: 'Delete my account',
    button: 'Delete my account',
    warning:
      'Removes everything above and the account itself. You will be signed out and cannot sign back in.',
  },
};

/**
 * Deletion is irreversible and reaches four places — the database, stored
 * receipt files, cached analyses and queued jobs. The flow therefore makes the
 * user type DELETE, and previews the damage first so nobody has to guess what
 * "everything" means.
 */
export function DangerZone() {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<Mode | null>(null);
  const [confirmation, setConfirmation] = useState('');
  const [preview, setPreview] = useState<DeletionResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setMode(null);
    setConfirmation('');
    setPreview(null);
    setError(null);
  }

  const dryRun = useMutation({
    mutationFn: (target: Mode) =>
      target === 'account' ? api.deleteAccount(true) : api.deleteData(true),
    onSuccess: setPreview,
    onError: (cause) =>
      setError(cause instanceof Error ? cause.message : 'Could not preview the deletion.'),
  });

  const destroy = useMutation({
    mutationFn: (target: Mode) =>
      target === 'account' ? api.deleteAccount(false) : api.deleteData(false),
    onSuccess: async (_result, target) => {
      if (target === 'account') {
        clearTokenCache();
        await signOut({ callbackUrl: '/sign-in' });
        return;
      }
      reset();
      await queryClient.invalidateQueries();
    },
    onError: (cause) =>
      setError(cause instanceof Error ? cause.message : 'The deletion did not complete.'),
  });

  const active = mode ? COPY[mode] : null;
  const canDelete = confirmation.trim() === 'DELETE';

  return (
    <Card className="border-negative/30">
      <CardHeader
        title="Delete data"
        subtitle="Permanent, and it reaches your stored files and cached analyses too"
        action={<Badge tone="negative">Irreversible</Badge>}
      />

      <div className="space-y-4 p-5">
        {mode === null ? (
          <div className="flex flex-wrap gap-2">
            {(['data', 'account'] as Mode[]).map((target) => (
              <button
                key={target}
                type="button"
                onClick={() => {
                  setMode(target);
                  setError(null);
                  dryRun.mutate(target);
                }}
                className="btn-secondary"
              >
                {COPY[target].title}
              </button>
            ))}
          </div>
        ) : (
          <div className="space-y-4">
            <div className="rounded-lg border border-negative/30 bg-negative/5 p-4">
              <p className="text-sm font-medium text-negative">{active?.title}</p>
              <p className="mt-1 text-xs leading-relaxed text-ink-muted">{active?.warning}</p>
            </div>

            {dryRun.isPending ? (
              <p className="flex items-center gap-2 text-sm text-ink-muted">
                <Spinner /> Working out exactly what would be removed…
              </p>
            ) : preview ? (
              <div data-testid="deletion-preview" className="rounded-lg border border-line bg-surface-sunken p-4">
                <p className="text-xs font-medium uppercase tracking-wide text-ink-muted">
                  This will remove
                </p>
                <dl className="mt-2 space-y-1">
                  {Object.entries(preview.rows_by_table)
                    .filter(([, count]) => count > 0)
                    .map(([table, count]) => (
                      <div key={table} className="grid grid-cols-[150px_1fr] gap-2">
                        <dt className="text-xs text-ink-faint">{table.replace(/_/g, ' ')}</dt>
                        <dd className="text-xs tabular-nums text-ink">{count}</dd>
                      </div>
                    ))}
                  {preview.queued_jobs_cancelled > 0 ? (
                    <div className="grid grid-cols-[150px_1fr] gap-2">
                      <dt className="text-xs text-ink-faint">queued jobs</dt>
                      <dd className="text-xs tabular-nums text-ink">
                        {preview.queued_jobs_cancelled} cancelled
                      </dd>
                    </div>
                  ) : null}
                </dl>
                <p className="mt-3 text-xs leading-relaxed text-ink-muted">
                  Stored receipt files and every cached analysis are removed as well.
                </p>
              </div>
            ) : null}

            <div>
              <label htmlFor="delete-confirm" className="label">
                Type <span className="font-mono text-ink">DELETE</span> to confirm
              </label>
              <input
                id="delete-confirm"
                value={confirmation}
                onChange={(event) => setConfirmation(event.target.value)}
                autoComplete="off"
                className={cn('input max-w-xs', canDelete && 'border-negative')}
              />
            </div>

            {error ? (
              <p role="alert" className="text-sm text-negative">
                {error}
              </p>
            ) : null}

            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                disabled={!canDelete || destroy.isPending}
                onClick={() => mode && destroy.mutate(mode)}
                className="btn inline-flex bg-negative text-white hover:opacity-90"
              >
                {destroy.isPending ? <Spinner /> : null}
                {active?.button}
              </button>
              <button type="button" onClick={reset} className="btn-ghost">
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}
