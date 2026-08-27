import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it } from 'vitest';

import { StepCard } from '@/components/ask/step-card';
import type { AnalysisStepEvent } from '@/lib/types';
import { useUiStore } from '@/stores/ui';

const completedAggregate: AnalysisStepEvent = {
  seq: 6,
  step: 'aggregate',
  status: 'completed',
  title: 'Computed total over 9 rows',
  duration_ms: 12,
  payload: {
    computation: 'SUM(amount) over spending, 2026-07-01 to 2026-07-31',
    computed_by: 'PostgreSQL aggregate over your transactions.',
    sql: 'SELECT sum(abs(transactions.amount_cents)) FROM transactions',
    result: {
      total: 703.93,
      rows: [{ label: 'Whole Foods MKT', value: 430.12, transaction_count: 5 }],
    },
  },
};

const runningStep: AnalysisStepEvent = {
  seq: 5,
  step: 'aggregate',
  status: 'started',
  title: 'Running a structured aggregation',
  duration_ms: 0,
  payload: {},
};

beforeEach(() => {
  useUiStore.setState({ expandedSteps: {} });
});

describe('StepCard', () => {
  it('shows the step name and the completed summary', () => {
    render(<StepCard step={completedAggregate} runId="run-1" isActive={false} />);
    expect(screen.getByText('Running a structured aggregation')).toBeInTheDocument();
    expect(screen.getByText('Computed total over 9 rows')).toBeInTheDocument();
    expect(screen.getByText('12 ms')).toBeInTheDocument();
  });

  it('starts collapsed and expands the payload on click', async () => {
    const user = userEvent.setup();
    render(<StepCard step={completedAggregate} runId="run-1" isActive={false} />);

    const toggle = screen.getByRole('button', { expanded: false });
    expect(screen.queryByText(/PostgreSQL aggregate/)).not.toBeInTheDocument();

    await user.click(toggle);

    expect(screen.getByRole('button', { expanded: true })).toBeInTheDocument();
    expect(screen.getByText(/PostgreSQL aggregate/)).toBeInTheDocument();
  });

  it('exposes the grouped result rows the number came from', async () => {
    const user = userEvent.setup();
    render(<StepCard step={completedAggregate} runId="run-1" isActive={false} />);

    await user.click(screen.getByRole('button'));

    expect(screen.getByText('Whole Foods MKT')).toBeInTheDocument();
    expect(screen.getByText('$430.12')).toBeInTheDocument();
  });

  it('exposes the SQL so the user can audit the computation', async () => {
    const user = userEvent.setup();
    render(<StepCard step={completedAggregate} runId="run-1" isActive={false} />);

    await user.click(screen.getByRole('button'));

    expect(
      screen.getByText('Show the SQL that produced these numbers'),
    ).toBeInTheDocument();
    expect(screen.getByText(/SELECT sum\(abs/)).toBeInTheDocument();
  });

  it('cannot be expanded while the step is still running', async () => {
    const user = userEvent.setup();
    render(<StepCard step={runningStep} runId="run-1" isActive />);

    const toggle = screen.getByRole('button');
    expect(toggle).toBeDisabled();
    await user.click(toggle);
    expect(screen.getByRole('button', { expanded: false })).toBeInTheDocument();
  });

  it('flags a narration whose numbers did not verify', async () => {
    const user = userEvent.setup();
    render(
      <StepCard
        runId="run-1"
        isActive={false}
        step={{
          seq: 10,
          step: 'explain',
          status: 'completed',
          title: 'Explanation prepared',
          duration_ms: 1,
          payload: {
            narrator: 'template',
            narrator_note: 'Written from a fixed template.',
            numeric_verification: {
              checked: true,
              passed: false,
              unverified_numbers: ['1204.99'],
              note: 'Checked against the computed result.',
            },
          },
        }}
      />,
    );

    await user.click(screen.getByRole('button'));
    expect(screen.getByText(/Unverified: 1204.99/)).toBeInTheDocument();
  });
});
