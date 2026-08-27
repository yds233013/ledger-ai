import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { FieldConfidence } from '@/components/receipts/confidence-chip';
import { ReceiptFieldEditor, draftFromReceipt } from '@/components/receipts/field-editor';
import type { Account, Category, ReceiptDetail } from '@/lib/types';

const categories: Category[] = [
  { id: 'cat-groceries', name: 'Groceries', slug: 'groceries', color: '#10b981', icon: 'cart' },
];
const accounts: Account[] = [
  {
    id: 'acct-1',
    name: 'SANDBOX — Everyday Checking',
    institution: 'Sandbox',
    account_type: 'checking',
    mask: '0001',
  },
  {
    id: 'acct-cash',
    name: 'Cash / Receipt Purchases',
    institution: 'Ledger AI (not a bank)',
    account_type: 'cash',
    mask: '0000',
  },
];

function makeReceipt(overrides: Partial<ReceiptDetail> = {}): ReceiptDetail {
  return {
    id: 'r-1',
    status: 'needs_review',
    merchant: 'Sandbox Grocers',
    posted_date: '2026-08-14',
    total_cents: 3036,
    total: 30.36,
    currency: 'USD',
    ocr_confidence: 0.93,
    needs_review: true,
    page_count: 1,
    original_filename: 'receipt_grocers_synthetic.png',
    content_type: 'image/png',
    transaction_id: null,
    link_mode: null,
    created_at: '2026-08-14T00:00:00Z',
    subtotal_cents: 2805,
    tax_cents: 231,
    tip_cents: 0,
    field_confidence: { merchant: 0.96, posted_date: 0.96, total: 1.0, tax: 0.6 },
    parse_notes: { consistency: 'Subtotal + tax + tip matches the total.' },
    raw_text: 'SANDBOX GROCERS\nTOTAL 30.36',
    currency_warning: null,
    base_currency: 'USD',
    categories,
    accounts,
    default_account_name: 'Cash / Receipt Purchases',
    ...overrides,
  };
}

function renderEditor(receipt = makeReceipt()) {
  const onChange = vi.fn();
  render(
    <ReceiptFieldEditor
      receipt={receipt}
      draft={draftFromReceipt(receipt)}
      onChange={onChange}
      accounts={receipt.accounts}
      categories={receipt.categories}
    />,
  );
  return onChange;
}

describe('draftFromReceipt', () => {
  it('renders integer cents as editable decimal strings', () => {
    const draft = draftFromReceipt(makeReceipt());
    expect(draft.total).toBe('30.36');
    expect(draft.subtotal).toBe('28.05');
    expect(draft.tip).toBe('0.00');
  });

  it('leaves missing fields blank rather than guessing zero', () => {
    const draft = draftFromReceipt(makeReceipt({ tax_cents: null, merchant: null }));
    expect(draft.tax).toBe('');
    expect(draft.merchant).toBe('');
  });
});

describe('FieldConfidence', () => {
  it.each([
    [0.96, 'high'],
    [0.8, 'medium'],
    [0.4, 'low — please check'],
  ])('maps %s to %s', (score, label) => {
    render(<FieldConfidence score={score} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });

  it('says when a field was not found at all', () => {
    render(<FieldConfidence score={undefined} />);
    expect(screen.getByText('not found')).toBeInTheDocument();
  });
});

describe('ReceiptFieldEditor', () => {
  it('shows every extracted field as editable', () => {
    renderEditor();
    expect(screen.getByLabelText(/Merchant/)).toHaveValue('Sandbox Grocers');
    expect(screen.getByLabelText(/Date/)).toHaveValue('2026-08-14');
    expect(screen.getByLabelText(/Total/)).toHaveValue('30.36');
  });

  it('lets the user correct a low-confidence field', async () => {
    const user = userEvent.setup();
    const onChange = renderEditor();

    await user.type(screen.getByLabelText(/Merchant/), '!');

    expect(onChange).toHaveBeenCalled();
  });

  it('states that confirming records an outflow', () => {
    renderEditor();
    expect(screen.getByText(/records money spent/)).toBeInTheDocument();
    expect(screen.getByText(/30\.36 USD/)).toBeInTheDocument();
  });

  it('defaults the account to the named synthetic one, not a bank account', () => {
    renderEditor();
    const select = screen.getByLabelText('Account') as HTMLSelectElement;
    expect(select.value).toBe('');
    expect(screen.getByText('Cash / Receipt Purchases (default)')).toBeInTheDocument();
    expect(
      screen.getByText(/rather than a bank account/),
    ).toBeInTheDocument();
  });

  it('offers the real accounts for explicit selection', () => {
    renderEditor();
    expect(
      screen.getByRole('option', { name: 'SANDBOX — Everyday Checking' }),
    ).toBeInTheDocument();
  });

  it('explains how the fields were read', async () => {
    const user = userEvent.setup();
    renderEditor();

    await user.click(screen.getByText('How these fields were read'));

    expect(screen.getByText(/matches the total/)).toBeInTheDocument();
  });

  it('exposes the raw OCR text with its confidence', () => {
    renderEditor();
    expect(screen.getByText('93% confidence')).toBeInTheDocument();
  });
});

/**
 * A confirmed receipt is immutable — the API refuses edits to it.
 *
 * Before this was pinned, every field stayed live on a confirmed receipt while
 * the save button was gone: a user could retype the whole form and have no way
 * to save it, and the summary still said confirming "creates" an outflow that
 * had already been created.
 */
describe('a confirmed receipt is read-only', () => {
  function renderConfirmed() {
    const receipt = makeReceipt({
      status: 'confirmed',
      transaction_id: 'tx-1',
      link_mode: 'created',
      needs_review: false,
    });
    render(
      <ReceiptFieldEditor
        receipt={receipt}
        draft={draftFromReceipt(receipt)}
        onChange={vi.fn()}
        accounts={accounts}
        categories={categories}
      />,
    );
  }

  it('disables every extracted field', () => {
    renderConfirmed();
    for (const id of [
      'receipt-merchant', 'receipt-date', 'receipt-currency',
      'receipt-subtotal', 'receipt-tax', 'receipt-tip', 'receipt-total',
    ]) {
      expect(document.getElementById(id)).toBeDisabled();
    }
  });

  it('disables the account and category pickers', () => {
    renderConfirmed();
    expect(document.getElementById('receipt-account')).toBeDisabled();
    expect(document.getElementById('receipt-category')).toBeDisabled();
  });

  it('never reports the change as still pending', () => {
    renderConfirmed();
    expect(screen.queryByText(/confirming it creates an outflow/i)).not.toBeInTheDocument();
    expect(screen.getByText(/was recorded as an outflow of/i)).toBeInTheDocument();
  });

  it('typing into a locked field cannot reach the change handler', async () => {
    const onChange = vi.fn();
    const receipt = makeReceipt({ status: 'confirmed', transaction_id: 'tx-1' });
    render(
      <ReceiptFieldEditor
        receipt={receipt}
        draft={draftFromReceipt(receipt)}
        onChange={onChange}
        accounts={accounts}
        categories={categories}
      />,
    );

    await userEvent.type(document.getElementById('receipt-merchant')!, 'Hijacked');
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('an unconfirmed receipt stays editable', () => {
  it('leaves the fields enabled', () => {
    // Positive control: the lock above must be caused by the status, not by
    // the fields being disabled for everyone.
    const receipt = makeReceipt({ status: 'needs_review' });
    render(
      <ReceiptFieldEditor
        receipt={receipt}
        draft={draftFromReceipt(receipt)}
        onChange={vi.fn()}
        accounts={accounts}
        categories={categories}
      />,
    );

    expect(document.getElementById('receipt-merchant')).toBeEnabled();
    expect(document.getElementById('receipt-total')).toBeEnabled();
    expect(document.getElementById('receipt-account')).toBeEnabled();
    expect(screen.getByText(/confirming it creates an outflow/i)).toBeInTheDocument();
  });
});
