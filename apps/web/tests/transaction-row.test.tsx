import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { TransactionRow } from '@/components/transactions/transaction-row';
import type { Category, CorrectionImpact, Transaction } from '@/lib/types';

const categories: Category[] = [
  { id: 'cat-groceries', name: 'Groceries', slug: 'groceries', color: '#10b981', icon: 'cart' },
  { id: 'cat-dining', name: 'Dining & Restaurants', slug: 'dining', color: '#f59e0b', icon: 'fork' },
  { id: 'cat-travel', name: 'Travel', slug: 'travel', color: '#0ea5e9', icon: 'plane' },
];

function makeTransaction(overrides: Partial<Transaction> = {}): Transaction {
  return {
    id: 'tx-1',
    posted_date: '2026-07-13',
    amount_cents: -13058,
    amount: -130.58,
    currency: 'USD',
    merchant: 'Whole Foods MKT',
    merchant_key: 'whole foods mkt',
    raw_description: 'WHOLE FOODS MKT 10233 AUSTIN TX [SYNTHETIC]',
    category: categories[0],
    confidence: 0.9,
    categorized_by: 'rule',
    needs_review: false,
    is_corrected: false,
    account_id: 'acct-1',
    account_name: 'SANDBOX — Everyday Checking',
    upload_id: null,
    created_at: '2026-07-13T00:00:00Z',
    ...overrides,
  };
}

function makeImpact(overrides: Partial<CorrectionImpact> = {}): CorrectionImpact {
  return {
    merchant: 'Whole Foods MKT',
    merchant_key: 'whole foods mkt',
    matching_count: 5,
    affected_count: 5,
    protected_count: 0,
    already_correct_count: 0,
    affected_ids: ['a', 'b', 'c', 'd', 'e'],
    ...overrides,
  };
}

function renderRow(props: Partial<Parameters<typeof TransactionRow>[0]> = {}) {
  const onCategoryChange = props.onCategoryChange ?? vi.fn();
  render(
    <table>
      <tbody>
        <TransactionRow
          transaction={makeTransaction()}
          categories={categories}
          onCategoryChange={onCategoryChange}
          {...props}
        />
      </tbody>
    </table>,
  );
  return onCategoryChange;
}

describe('TransactionRow', () => {
  it('renders the merchant, date and amount as a positive magnitude with a sign', () => {
    renderRow();
    expect(screen.getByText('Whole Foods MKT')).toBeInTheDocument();
    expect(screen.getByText('Jul 13')).toBeInTheDocument();
    expect(screen.getByText(/\$130\.58/)).toBeInTheDocument();
  });

  it('marks income with a plus and the positive tone', () => {
    renderRow({
      transaction: makeTransaction({ amount_cents: 612500, amount: 6125, merchant: 'Payroll' }),
    });
    expect(screen.getByText(/\+/)).toBeInTheDocument();
    expect(screen.getByText(/\$6,125\.00/)).toBeInTheDocument();
  });

  it('calls back with the chosen category when edited', async () => {
    const user = userEvent.setup();
    const onCategoryChange = renderRow();

    await user.selectOptions(screen.getByRole('combobox'), 'cat-dining');

    expect(onCategoryChange).toHaveBeenCalledWith('tx-1', 'cat-dining');
  });

  it('surfaces the review flag and a low-confidence indicator', () => {
    renderRow({
      transaction: makeTransaction({
        category: null,
        confidence: 0,
        categorized_by: 'none',
        needs_review: true,
      }),
    });
    expect(screen.getByText('Review')).toBeInTheDocument();
    expect(screen.getByText('Low')).toBeInTheDocument();
  });

  it('explains why a category was assigned', () => {
    renderRow({ transaction: makeTransaction({ categorized_by: 'correction', confidence: 1 }) });
    expect(
      screen.getByTitle(/You corrected this merchant before \(confidence 1\.00\)/),
    ).toBeInTheDocument();
  });

  it('marks a manually edited transaction', () => {
    renderRow({ transaction: makeTransaction({ is_corrected: true }) });
    expect(screen.getByText('Edited')).toBeInTheDocument();
  });

  it('disables the category control while a save is in flight', () => {
    renderRow({ isSaving: true });
    expect(screen.getByRole('combobox')).toBeDisabled();
  });

  it('shows no confirmation row until a correction is pending', () => {
    renderRow();
    expect(screen.queryByTestId('correction-confirm')).not.toBeInTheDocument();
  });
});

describe('TransactionRow — correction confirmation', () => {
  const pending = { categoryId: 'cat-travel', applyToMatching: true };

  it('offers "apply to all matching", selected by default', () => {
    renderRow({ pending, impact: makeImpact() });

    const checkbox = screen.getByRole('checkbox', { name: /Apply to all matching transactions/ });
    expect(checkbox).toBeChecked();
  });

  it('shows how many transactions would be affected before confirming', () => {
    renderRow({ pending, impact: makeImpact({ affected_count: 5 }) });

    const confirm = screen.getByTestId('correction-confirm');
    expect(within(confirm).getByText(/5 other transactions from Whole Foods MKT/)).toBeInTheDocument();
    // The button states the full blast radius: 5 siblings plus this row.
    expect(screen.getByRole('button', { name: 'Apply to 6 transactions' })).toBeInTheDocument();
  });

  it('uses the singular form for a single sibling', () => {
    renderRow({ pending, impact: makeImpact({ affected_count: 1, affected_ids: ['a'] }) });
    expect(screen.getByText(/1 other transaction from/)).toBeInTheDocument();
  });

  it('reports how many rows are protected from the bulk change', () => {
    renderRow({ pending, impact: makeImpact({ affected_count: 4, protected_count: 1 }) });
    expect(
      screen.getByText(/1 transaction was edited individually before and will be left unchanged/),
    ).toBeInTheDocument();
  });

  it('lets the user turn the retroactive option off', async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    renderRow({ pending, impact: makeImpact(), onToggleApplyToMatching: onToggle });

    await user.click(screen.getByRole('checkbox', { name: /Apply to all matching/ }));

    expect(onToggle).toHaveBeenCalledWith(false);
  });

  it('falls back to a single-row label when retroactive is off', () => {
    renderRow({
      pending: { categoryId: 'cat-travel', applyToMatching: false },
      impact: makeImpact(),
    });
    expect(screen.getByRole('button', { name: 'Apply to this one' })).toBeInTheDocument();
    expect(screen.getByRole('checkbox')).not.toBeChecked();
  });

  it('waits for the count before allowing confirmation', () => {
    renderRow({ pending, impact: null, impactLoading: true });

    expect(screen.getByText(/counting…/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Apply/ })).toBeDisabled();
  });

  it('confirms and cancels through their callbacks', async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const onCancel = vi.fn();
    renderRow({ pending, impact: makeImpact(), onConfirm, onCancel });

    await user.click(screen.getByRole('button', { name: /Apply to 6 transactions/ }));
    expect(onConfirm).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('explains when there is nothing left to change', () => {
    renderRow({
      pending,
      impact: makeImpact({ affected_count: 0, matching_count: 3, already_correct_count: 3, affected_ids: [] }),
    });
    expect(
      screen.getByText(/already has this category or was edited individually/),
    ).toBeInTheDocument();
  });

  it('previews the pending category in the select and colour dot', () => {
    renderRow({ pending, impact: makeImpact() });
    expect(screen.getByRole('combobox')).toHaveValue('cat-travel');
  });
});
