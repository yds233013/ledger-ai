'use client';

import { Badge, Spinner } from '@/components/ui/primitives';
import { cn } from '@/lib/cn';
import { formatDuration, formatMoney } from '@/lib/format';
import type { AnalysisStepEvent, AnalysisStepName } from '@/lib/types';
import { useUiStore } from '@/stores/ui';

const STEP_LABELS: Record<AnalysisStepName, string> = {
  understand: 'Understanding the question',
  select: 'Selecting relevant transactions',
  aggregate: 'Running a structured aggregation',
  visualize: 'Generating a chart',
  explain: 'Preparing the explanation',
};

const STEP_ORDER: AnalysisStepName[] = [
  'understand',
  'select',
  'aggregate',
  'visualize',
  'explain',
];

export { STEP_LABELS, STEP_ORDER };

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[110px_1fr] gap-3 py-1">
      <dt className="text-xs text-ink-faint">{label}</dt>
      <dd className="text-xs text-ink">{children}</dd>
    </div>
  );
}

function Pre({ children }: { children: string }) {
  return (
    <pre className="max-h-56 overflow-auto rounded-md bg-surface-sunken p-2.5 font-mono text-[11px] leading-relaxed text-ink-muted">
      {children}
    </pre>
  );
}

/** Render the step's inspectable body. Each step exposes different evidence. */
function StepBody({ step }: { step: AnalysisStepEvent }) {
  const payload = step.payload as Record<string, never>;

  if (step.step === 'understand') {
    if (payload.declined) {
      return (
        <p className="text-xs leading-relaxed text-ink-muted">{String(payload.reason ?? '')}</p>
      );
    }
    const interpretation = payload.interpretation as
      | { intent?: string; period?: string; filters?: string[]; assumptions?: string[] }
      | undefined;

    return (
      <div className="space-y-3">
        <dl>
          <Row label="Interpreted as">{interpretation?.intent ?? '—'}</Row>
          <Row label="Time period">{interpretation?.period ?? '—'}</Row>
          <Row label="Filters">
            <ul className="space-y-0.5">
              {(interpretation?.filters ?? []).map((filter) => (
                <li key={filter}>{filter}</li>
              ))}
            </ul>
          </Row>
          {interpretation?.assumptions?.length ? (
            <Row label="Assumptions">
              <ul className="list-inside list-disc space-y-0.5 text-ink-muted">
                {interpretation.assumptions.map((assumption) => (
                  <li key={assumption}>{assumption}</li>
                ))}
              </ul>
            </Row>
          ) : null}
        </dl>
        <p className="text-[11px] leading-relaxed text-ink-faint">
          {String(payload.planner_note ?? '')}
        </p>
        <details>
          <summary className="cursor-pointer text-xs text-brand hover:underline">
            Show the resolved query plan
          </summary>
          <div className="mt-2">
            <Pre>{JSON.stringify(payload.plan, null, 2)}</Pre>
          </div>
        </details>
      </div>
    );
  }

  if (step.step === 'select') {
    const range = payload.date_range as { start: string; end: string; label: string } | undefined;
    const observed = payload.observed_range as { first: string | null; last: string | null } | undefined;
    return (
      <div className="space-y-3">
        <dl>
          <Row label="Matched">
            <span className="font-medium tabular-nums">
              {String(payload.matched_transactions ?? 0)} transactions
            </span>
          </Row>
          <Row label="Window">
            {range ? `${range.start} → ${range.end} (${range.label})` : '—'}
          </Row>
          {observed?.first ? (
            <Row label="Actual data">
              {observed.first} → {observed.last}
            </Row>
          ) : null}
          <Row label="Filters">
            <ul className="space-y-0.5">
              {((payload.filters_applied as string[]) ?? []).map((filter) => (
                <li key={filter}>{filter}</li>
              ))}
            </ul>
          </Row>
        </dl>
        <details>
          <summary className="cursor-pointer text-xs text-brand hover:underline">
            Show the selection query
          </summary>
          <div className="mt-2">
            <Pre>{String(payload.sql ?? '')}</Pre>
          </div>
        </details>
      </div>
    );
  }

  if (step.step === 'aggregate') {
    const result = payload.result as
      | { rows?: { label: string; value: number; transaction_count: number }[]; total?: number }
      | undefined;
    const rows = result?.rows ?? [];

    return (
      <div className="space-y-3">
        <dl>
          <Row label="Computation">{String(payload.computation ?? '')}</Row>
          <Row label="Computed by">
            <span className="text-ink-muted">{String(payload.computed_by ?? '')}</span>
          </Row>
        </dl>

        {rows.length ? (
          <div className="overflow-hidden rounded-md border border-line">
            <table className="w-full text-xs">
              <thead>
                <tr className="border-b border-line bg-surface-sunken text-left">
                  <th className="px-2.5 py-1.5 font-medium text-ink-muted">Group</th>
                  <th className="px-2.5 py-1.5 text-right font-medium text-ink-muted">Amount</th>
                  <th className="px-2.5 py-1.5 text-right font-medium text-ink-muted">Count</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 12).map((row) => (
                  <tr key={row.label} className="border-b border-line last:border-0">
                    <td className="px-2.5 py-1.5 text-ink">{row.label}</td>
                    <td className="px-2.5 py-1.5 text-right tabular-nums text-ink">
                      {formatMoney(row.value)}
                    </td>
                    <td className="px-2.5 py-1.5 text-right tabular-nums text-ink-muted">
                      {row.transaction_count}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}

        <details>
          <summary className="cursor-pointer text-xs text-brand hover:underline">
            Show the SQL that produced these numbers
          </summary>
          <div className="mt-2">
            <Pre>{String(payload.sql ?? '')}</Pre>
          </div>
        </details>
      </div>
    );
  }

  if (step.step === 'visualize') {
    const chart = payload.chart as { kind?: string; title?: string; data?: unknown[] } | undefined;
    return (
      <dl>
        <Row label="Chart type">{chart?.kind ?? 'none'}</Row>
        <Row label="Title">{chart?.title || '—'}</Row>
        <Row label="Data points">{chart?.data?.length ?? 0}</Row>
      </dl>
    );
  }

  // explain
  const verification = payload.numeric_verification as
    | { checked: boolean; passed: boolean; unverified_numbers: string[]; note: string }
    | undefined;

  return (
    <div className="space-y-3">
      <dl>
        <Row label="Written by">{String(payload.narrator ?? 'template')}</Row>
        <Row label="Numbers">
          {verification?.passed ? (
            <Badge tone="positive">All verified against the computed result</Badge>
          ) : (
            <Badge tone="negative">
              Unverified: {verification?.unverified_numbers.join(', ')}
            </Badge>
          )}
        </Row>
      </dl>
      <p className="text-[11px] leading-relaxed text-ink-faint">
        {String(payload.narrator_note ?? '')} {verification?.note}
      </p>
    </div>
  );
}

export function StepCard({
  step,
  runId,
  isActive,
}: {
  step: AnalysisStepEvent;
  runId: string;
  isActive: boolean;
}) {
  const key = `${runId}:${step.step}`;
  const expanded = useUiStore((state) => state.expandedSteps[key] ?? false);
  const toggleStep = useUiStore((state) => state.toggleStep);
  const done = step.status === 'completed';

  return (
    <li className="animate-fade-up">
      <div
        className={cn(
          'rounded-lg border transition-colors',
          done ? 'border-line bg-surface-raised' : 'border-brand/30 bg-brand-soft/30',
        )}
      >
        <button
          type="button"
          onClick={() => done && toggleStep(key)}
          aria-expanded={expanded}
          disabled={!done}
          className="flex w-full items-center gap-2.5 px-3.5 py-2.5 text-left disabled:cursor-default"
        >
          <span
            aria-hidden="true"
            className={cn(
              'grid h-5 w-5 shrink-0 place-items-center rounded-full text-[10px] font-semibold',
              done ? 'bg-positive/15 text-positive' : 'bg-brand/15 text-brand',
            )}
          >
            {done ? '✓' : <Spinner className="h-3 w-3" />}
          </span>

          <span className="min-w-0 flex-1">
            <span className="block text-xs font-medium text-ink">
              {STEP_LABELS[step.step]}
            </span>
            {done ? (
              <span className="mt-0.5 block truncate text-xs text-ink-muted">{step.title}</span>
            ) : null}
          </span>

          {done ? (
            <>
              <span className="shrink-0 text-[11px] tabular-nums text-ink-faint">
                {formatDuration(step.duration_ms)}
              </span>
              <span aria-hidden="true" className="shrink-0 text-ink-faint">
                {expanded ? '▾' : '▸'}
              </span>
            </>
          ) : isActive ? (
            <span className="shrink-0 text-[11px] text-brand">running…</span>
          ) : null}
        </button>

        {done && expanded ? (
          <div className="border-t border-line px-3.5 py-3">
            <StepBody step={step} />
          </div>
        ) : null}
      </div>
    </li>
  );
}
