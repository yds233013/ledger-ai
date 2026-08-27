import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { RefinementChips } from '@/components/ask/refinement-chips';
import type { RefinementChip } from '@/lib/types';

const chips: RefinementChip[] = [
  {
    key: 'group_by_merchant',
    label: 'Break down by merchant',
    description: 'Same period and filters, grouped by merchant instead.',
  },
  {
    key: 'compare_previous_period',
    label: 'Compare with the previous period',
    description: 'Adds the preceding period as a baseline.',
  },
  {
    key: 'only:Groceries',
    label: 'Only Groceries',
    description: 'Narrow this to Groceries and drop the grouping.',
  },
];

describe('RefinementChips', () => {
  it('offers each follow-up as an explicit named action', () => {
    render(<RefinementChips chips={chips} onRefine={vi.fn()} />);
    for (const chip of chips) {
      expect(screen.getByRole('button', { name: chip.label })).toBeInTheDocument();
    }
  });

  it('passes the whole chip back so the caller sends its key, not free text', async () => {
    const user = userEvent.setup();
    const onRefine = vi.fn();
    render(<RefinementChips chips={chips} onRefine={onRefine} />);

    await user.click(screen.getByRole('button', { name: 'Break down by merchant' }));

    expect(onRefine).toHaveBeenCalledWith(chips[0]);
  });

  it('states that nothing is inferred from conversation history', () => {
    render(<RefinementChips chips={chips} onRefine={vi.fn()} />);
    expect(
      screen.getByText(/nothing is inferred from conversation history/i),
    ).toBeInTheDocument();
  });

  it('describes what each refinement will do', () => {
    render(<RefinementChips chips={chips} onRefine={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Only Groceries' })).toHaveAttribute(
      'title',
      'Narrow this to Groceries and drop the grouping.',
    );
  });

  it('renders nothing when no refinement applies', () => {
    const { container } = render(<RefinementChips chips={[]} onRefine={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('is inert while an analysis is streaming', () => {
    render(<RefinementChips chips={chips} onRefine={vi.fn()} disabled />);
    for (const chip of chips) {
      expect(screen.getByRole('button', { name: chip.label })).toBeDisabled();
    }
  });
});
