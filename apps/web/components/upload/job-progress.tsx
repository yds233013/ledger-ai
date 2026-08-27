'use client';

import { Badge, Spinner } from '@/components/ui/primitives';
import { cn } from '@/lib/cn';
import type { JobStage, ProcessingJob } from '@/lib/types';

const STAGES: { key: JobStage; label: string; detail: string }[] = [
  { key: 'queued', label: 'Queued', detail: 'Waiting for a worker' },
  { key: 'extracting', label: 'Extracting', detail: 'Reading rows from the file' },
  { key: 'normalizing', label: 'Normalizing', detail: 'Parsing dates, amounts and merchants' },
  { key: 'categorizing', label: 'Categorizing', detail: 'Applying rules and your corrections' },
  { key: 'analyzing', label: 'Analyzing', detail: 'Checking for duplicates and unusual charges' },
  { key: 'complete', label: 'Complete', detail: 'Finished' },
];

function stageIndex(stage: JobStage): number {
  const index = STAGES.findIndex((item) => item.key === stage);
  return index === -1 ? STAGES.length - 1 : index;
}

export function JobProgress({ job }: { job: ProcessingJob }) {
  const failed = job.stage === 'failed';
  const current = stageIndex(job.stage);

  return (
    <div className="space-y-4">
      <div>
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="font-medium text-ink">
            {failed ? 'Processing failed' : STAGES[current]?.label}
          </span>
          <span className="tabular-nums text-ink-muted">{job.progress}%</span>
        </div>
        <div
          role="progressbar"
          aria-valuenow={job.progress}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="Processing progress"
          className="h-1.5 w-full overflow-hidden rounded-full bg-surface-sunken"
        >
          <div
            className={cn(
              'h-full rounded-full transition-all duration-500',
              failed ? 'bg-negative' : 'bg-brand',
            )}
            style={{ width: `${Math.max(job.progress, 4)}%` }}
          />
        </div>
      </div>

      <ol className="space-y-2">
        {STAGES.map((stage, index) => {
          const done = !failed && index < current;
          const active = !failed && index === current && job.stage !== 'complete';
          const isComplete = job.stage === 'complete' && stage.key === 'complete';

          return (
            <li key={stage.key} className="flex items-start gap-2.5">
              <span
                aria-hidden="true"
                className={cn(
                  'mt-0.5 grid h-4 w-4 shrink-0 place-items-center rounded-full text-[10px]',
                  done || isComplete
                    ? 'bg-positive/15 text-positive'
                    : active
                      ? 'bg-brand/15 text-brand'
                      : 'bg-surface-sunken text-ink-faint',
                )}
              >
                {active ? <Spinner className="h-2.5 w-2.5" /> : done || isComplete ? '✓' : '○'}
              </span>
              <div className="min-w-0">
                <p
                  className={cn(
                    'text-xs font-medium',
                    done || isComplete || active ? 'text-ink' : 'text-ink-faint',
                  )}
                >
                  {stage.label}
                </p>
                {active ? <p className="text-xs text-ink-muted">{stage.detail}</p> : null}
              </div>
            </li>
          );
        })}
      </ol>

      {job.stage === 'complete' ? (
        <div className="rounded-lg border border-line bg-surface-sunken p-3">
          <div className="flex flex-wrap gap-2">
            <Badge tone="positive">{job.rows_imported} imported</Badge>
            {job.rows_skipped > 0 ? <Badge tone="neutral">{job.rows_skipped} skipped</Badge> : null}
            <Badge tone="neutral">{job.rows_total} rows read</Badge>
          </div>
          {job.rows_skipped > 0 ? (
            <p className="mt-2 text-xs text-ink-muted">
              Skipped rows were either already imported or could not be parsed.
              {job.error_message ? ` ${job.error_message}` : ''}
            </p>
          ) : null}
        </div>
      ) : null}

      {failed ? (
        <div role="alert" className="rounded-lg border border-negative/30 bg-negative/5 p-3">
          <p className="text-xs font-medium text-negative">Processing failed</p>
          <p className="mt-1 text-xs text-ink-muted">
            {job.error_message ?? 'An unexpected error occurred.'}
          </p>
        </div>
      ) : null}
    </div>
  );
}
