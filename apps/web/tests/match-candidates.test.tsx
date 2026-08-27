import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { MatchCandidates } from '@/components/receipts/match-candidates';
import type { MatchCandidate } from '@/lib/types';

function makeCandidate(overrides: Partial<MatchCandidate> = {}): MatchCandidate {
  return {
    transaction_id: 'tx-1',
    posted_date: '2026-08-16',
    merchant: 'Sandbox Coffee House',
    amount_cents: -1092,
    amount: -10.92,
    currency: 'USD',
    account_id: 'acct-1',
    account_name: 'SANDBOX — Everyday Checking',
    category: 'Dining & Restaurants',
    source_upload_id: 'up-1',
    source_filename: 'august_statement_synthetic.csv',
    score: 1.0,
    signals: [
      { name: 'amount', detail: 'Exact match: both are 10.92 USD', contribution: 0.5 },
      { name: 'date', detail: 'Same day as the receipt', contribution: 0.3 },
      { name: 'merchant', detail: '“Sandbox Coffee House” is 100% similar', contribution: 0.2 },
      { name: 'currency', detail: 'Both are in USD', contribution: 0 },
      { name: 'account', detail: 'Charged to SANDBOX — Everyday Checking', contribution: 0 },
    ],
    ...overrides,
  };
}

function renderPanel(props: Partial<Parameters<typeof MatchCandidates>[0]> = {}) {
  const onLink = props.onLink ?? vi.fn();
  const onReject = props.onReject ?? vi.fn();
  render(
    <MatchCandidates
      candidates={[makeCandidate()]}
      isLoading={false}
      note="These are suggestions only. Nothing is linked until you choose a transaction and confirm."
      linkingId={null}
      {...props}
      onLink={onLink}
      onReject={onReject}
    />,
  );
  return { onLink, onReject };
}

describe('MatchCandidates', () => {
  it('shows account, date, merchant, amount and source upload before linking', () => {
    renderPanel();
    expect(screen.getByText('Sandbox Coffee House')).toBeInTheDocument();
    expect(screen.getByText(/Aug 16/)).toBeInTheDocument();
    expect(screen.getByText(/\$10\.92/)).toBeInTheDocument();
    // Appears both in the summary line and in the account signal.
    expect(screen.getAllByText(/SANDBOX — Everyday Checking/).length).toBeGreaterThan(0);
    // The filename sits in a text node beside the category, so match on the
    // element's full text content rather than a single node.
    expect(
      screen.getByText(
        (_content, element) =>
          element?.tagName === 'P' &&
          (element.textContent ?? '').includes('august_statement_synthetic.csv'),
      ),
    ).toBeInTheDocument();
  });

  it('explains why each match was suggested', async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByText('Why this was suggested'));

    expect(screen.getByText(/Exact match: both are 10.92 USD/)).toBeInTheDocument();
    expect(screen.getByText(/Same day as the receipt/)).toBeInTheDocument();
    expect(screen.getByText(/Both are in USD/)).toBeInTheDocument();
  });

  it('never links without an explicit click', () => {
    const { onLink } = renderPanel();
    expect(onLink).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: /Link to this transaction/ })).toBeInTheDocument();
  });

  it('links only when the user confirms that candidate', async () => {
    const user = userEvent.setup();
    const { onLink } = renderPanel();

    await user.click(screen.getByRole('button', { name: /Link to this transaction/ }));

    expect(onLink).toHaveBeenCalledWith('tx-1');
  });

  it('lets the user reject a suggestion', async () => {
    const user = userEvent.setup();
    const { onReject } = renderPanel();

    await user.click(screen.getByRole('button', { name: 'Not this one' }));

    expect(onReject).toHaveBeenCalledWith('tx-1');
  });

  it('states that linking is non-destructive', () => {
    renderPanel();
    expect(
      screen.getByText(/does not change this transaction's merchant, category or amount/i),
    ).toBeInTheDocument();
  });

  it('says so plainly when there is no match', () => {
    renderPanel({ candidates: [] });
    expect(screen.getByText('No matching transaction found')).toBeInTheDocument();
    expect(screen.getByText(/will create a new transaction/)).toBeInTheDocument();
  });

  it('shows a loading state while candidates are being computed', () => {
    renderPanel({ isLoading: true });
    expect(screen.getByText(/Looking for a matching transaction/)).toBeInTheDocument();
  });

  it('disables both actions while a link is in flight', () => {
    renderPanel({ linkingId: 'tx-1' });
    expect(screen.getByRole('button', { name: /Link to this transaction/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Not this one' })).toBeDisabled();
  });
});
