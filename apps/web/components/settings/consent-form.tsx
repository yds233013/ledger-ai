'use client';

import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';

import { Spinner } from '@/components/ui/primitives';
import { api } from '@/lib/api-client';
import { CONSENT_COPY, CONSENT_ORDER, isReprompt } from '@/lib/consent';
import { queryKeys } from '@/lib/query-keys';
import type { ConsentState } from '@/lib/types';

/**
 * The checkboxes themselves, shared by the settings panel and the upload gate.
 *
 * Every box starts unchecked and nothing is pre-selected. A pre-ticked consent
 * box records an agreement the person never made, which is the failure this
 * whole flow exists to avoid — so the submit button stays disabled until each
 * outstanding document has been ticked deliberately.
 */
export function ConsentForm({
  state,
  onAccepted,
  submitLabel = 'Accept and continue',
}: {
  state: ConsentState;
  onAccepted?: () => void;
  submitLabel?: string;
}) {
  const queryClient = useQueryClient();
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  const pending = CONSENT_ORDER.filter((type) => state.missing.includes(type));
  const allTicked = pending.every((type) => checked[type]);

  const accept = useMutation({
    mutationFn: () => api.acceptConsents(pending),
    onSuccess: () => {
      setError(null);
      setChecked({});
      void queryClient.invalidateQueries({ queryKey: queryKeys.consents });
      onAccepted?.();
    },
    onError: (cause) =>
      setError(cause instanceof Error ? cause.message : 'That could not be recorded.'),
  });

  if (pending.length === 0) return null;

  return (
    <div className="space-y-4">
      {pending.map((type) => {
        const copy = CONSENT_COPY[type];
        const reprompt = isReprompt(state, type);
        return (
          <div key={type} className="rounded-lg border border-line bg-surface-sunken p-4">
            <label className="flex items-start gap-3">
              <input
                type="checkbox"
                checked={checked[type] ?? false}
                onChange={(event) =>
                  setChecked((current) => ({
                    ...current,
                    [type]: event.target.checked,
                  }))
                }
                className="mt-1 size-4 shrink-0 rounded border-line accent-brand"
                data-testid={`consent-checkbox-${type}`}
              />
              <span className="min-w-0">
                <span className="block text-sm font-medium text-ink">
                  {copy.label}
                  <span className="ml-2 font-mono text-xs font-normal text-ink-faint">
                    v{state.required[type]}
                  </span>
                </span>
                <span className="mt-0.5 block text-xs text-ink-muted">{copy.summary}</span>
              </span>
            </label>

            <ul className="mt-3 space-y-1.5 border-t border-line pt-3 pl-7 text-xs leading-relaxed text-ink-muted">
              {copy.points.map((point) => (
                <li key={point}>{point}</li>
              ))}
            </ul>

            {reprompt ? (
              <p className="mt-3 pl-7 text-xs text-ink-faint">
                You accepted v{state.accepted[type]} of this document. It has changed since, so this
                asks again rather than assuming the earlier answer still stands.
              </p>
            ) : null}
          </div>
        );
      })}

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => accept.mutate()}
          disabled={!allTicked || accept.isPending}
          className="btn-primary"
          data-testid="consent-submit"
        >
          {accept.isPending ? <Spinner /> : null}
          {accept.isPending ? 'Recording…' : submitLabel}
        </button>
        {!allTicked ? (
          <span className="text-xs text-ink-faint">Tick each box above to continue.</span>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="text-sm text-negative">
          {error}
        </p>
      ) : null}
    </div>
  );
}
