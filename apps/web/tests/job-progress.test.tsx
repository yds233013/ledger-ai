import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { JobProgress } from '@/components/upload/job-progress';
import type { JobStage, ProcessingJob } from '@/lib/types';

function makeJob(stage: JobStage, overrides: Partial<ProcessingJob> = {}): ProcessingJob {
  const progress: Record<JobStage, number> = {
    queued: 0, extracting: 20, normalizing: 45, categorizing: 65,
    analyzing: 85, complete: 100, failed: 100,
  };
  return {
    id: 'job-1',
    upload_id: 'upload-1',
    stage,
    progress: progress[stage],
    rows_total: 47,
    rows_imported: 47,
    rows_skipped: 0,
    error_message: null,
    started_at: null,
    finished_at: null,
    ...overrides,
  };
}

describe('JobProgress', () => {
  it('renders every pipeline stage so the user sees the whole shape', () => {
    render(<JobProgress job={makeJob('extracting')} />);
    // Scoped to the stage list: the active stage name also appears in the
    // progress-bar header above it.
    const stages = screen.getByRole('list');
    for (const label of [
      'Queued', 'Extracting', 'Normalizing', 'Categorizing', 'Analyzing', 'Complete',
    ]) {
      expect(within(stages).getByText(label)).toBeInTheDocument();
    }
  });

  it('reports progress accessibly', () => {
    render(<JobProgress job={makeJob('normalizing')} />);
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '45');
  });

  it('shows the duplicate-detection stage', () => {
    render(<JobProgress job={makeJob('analyzing')} />);
    const stages = screen.getByRole('list');
    expect(within(stages).getByText('Analyzing')).toBeInTheDocument();
    expect(
      screen.getByText('Checking for duplicates and unusual charges'),
    ).toBeInTheDocument();
  });

  it('summarizes the import once complete', () => {
    render(<JobProgress job={makeJob('complete')} />);
    expect(screen.getByText('47 imported')).toBeInTheDocument();
    expect(screen.getByText('47 rows read')).toBeInTheDocument();
  });

  it('explains skipped rows rather than hiding them', () => {
    render(
      <JobProgress
        job={makeJob('complete', {
          rows_imported: 1,
          rows_skipped: 47,
          error_message: '1 row(s) skipped — row 4: Unrecognized date format',
        })}
      />,
    );
    expect(screen.getByText('47 skipped')).toBeInTheDocument();
    expect(screen.getByText(/already imported or could not be parsed/)).toBeInTheDocument();
  });

  it('surfaces a failure with its message', () => {
    render(
      <JobProgress
        job={makeJob('failed', {
          error_message: 'Receipt image processing ships in Phase 2.',
        })}
      />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Receipt image processing ships in Phase 2.');
  });
});
