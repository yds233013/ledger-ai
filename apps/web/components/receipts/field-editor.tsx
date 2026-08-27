'use client';

import { Badge } from '@/components/ui/primitives';
import type { Account, Category, ReceiptDetail } from '@/lib/types';

import { FieldConfidence } from './confidence-chip';

export interface ReceiptDraft {
  merchant: string;
  posted_date: string;
  subtotal: string;
  tax: string;
  tip: string;
  total: string;
  currency: string;
  accountId: string;
  categoryId: string;
}

export function draftFromReceipt(receipt: ReceiptDetail): ReceiptDraft {
  const money = (cents: number | null) => (cents === null ? '' : (cents / 100).toFixed(2));
  return {
    merchant: receipt.merchant ?? '',
    posted_date: receipt.posted_date ?? '',
    subtotal: money(receipt.subtotal_cents),
    tax: money(receipt.tax_cents),
    tip: money(receipt.tip_cents),
    total: money(receipt.total_cents),
    currency: receipt.currency,
    accountId: '',
    categoryId: '',
  };
}

function MoneyField({
  id,
  label,
  value,
  confidence,
  onChange,
  required = false,
}: {
  id: string;
  label: string;
  value: string;
  confidence: number | undefined;
  onChange: (value: string) => void;
  required?: boolean;
}) {
  return (
    <div>
      <div className="mb-1.5 flex items-center justify-between gap-2">
        <label htmlFor={id} className="text-xs font-medium uppercase tracking-wide text-ink-muted">
          {label}
          {required ? <span className="text-negative"> *</span> : null}
        </label>
        <FieldConfidence score={confidence} />
      </div>
      <input
        id={id}
        inputMode="decimal"
        value={value}
        onChange={(event) => onChange(event.target.value)}
        placeholder="0.00"
        className="input text-right tabular-nums"
      />
    </div>
  );
}

export function ReceiptFieldEditor({
  receipt,
  draft,
  onChange,
  accounts,
  categories,
}: {
  receipt: ReceiptDetail;
  draft: ReceiptDraft;
  onChange: (patch: Partial<ReceiptDraft>) => void;
  accounts: Account[];
  categories: Category[];
}) {
  const confidence = receipt.field_confidence;

  return (
    <div className="space-y-4">
      <div>
        <div className="mb-1.5 flex items-center justify-between gap-2">
          <label htmlFor="receipt-merchant" className="text-xs font-medium uppercase tracking-wide text-ink-muted">
            Merchant <span className="text-negative">*</span>
          </label>
          <FieldConfidence score={confidence.merchant} />
        </div>
        <input
          id="receipt-merchant"
          value={draft.merchant}
          onChange={(event) => onChange({ merchant: event.target.value })}
          className="input"
        />
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <label htmlFor="receipt-date" className="text-xs font-medium uppercase tracking-wide text-ink-muted">
              Date <span className="text-negative">*</span>
            </label>
            <FieldConfidence score={confidence.posted_date} />
          </div>
          <input
            id="receipt-date"
            type="date"
            value={draft.posted_date}
            onChange={(event) => onChange({ posted_date: event.target.value })}
            className="input"
          />
        </div>
        <div>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <label htmlFor="receipt-currency" className="text-xs font-medium uppercase tracking-wide text-ink-muted">
              Currency
            </label>
            <FieldConfidence score={confidence.currency} />
          </div>
          <input
            id="receipt-currency"
            maxLength={3}
            value={draft.currency}
            onChange={(event) => onChange({ currency: event.target.value.toUpperCase() })}
            className="input uppercase"
          />
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <MoneyField
          id="receipt-subtotal" label="Subtotal" value={draft.subtotal}
          confidence={confidence.subtotal} onChange={(v) => onChange({ subtotal: v })}
        />
        <MoneyField
          id="receipt-tax" label="Tax" value={draft.tax}
          confidence={confidence.tax} onChange={(v) => onChange({ tax: v })}
        />
        <MoneyField
          id="receipt-tip" label="Tip" value={draft.tip}
          confidence={confidence.tip} onChange={(v) => onChange({ tip: v })}
        />
        <MoneyField
          id="receipt-total" label="Total" value={draft.total} required
          confidence={confidence.total} onChange={(v) => onChange({ total: v })}
        />
      </div>

      <div className="rounded-lg border border-line bg-surface-sunken p-3">
        <p className="text-xs leading-relaxed text-ink-muted">
          This receipt records money spent, so confirming it creates an outflow of{' '}
          <span className="font-medium text-ink">
            {draft.total || '0.00'} {draft.currency}
          </span>
          .
        </p>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label htmlFor="receipt-account" className="label">
            Account
          </label>
          <select
            id="receipt-account"
            value={draft.accountId}
            onChange={(event) => onChange({ accountId: event.target.value })}
            className="input"
          >
            <option value="">{receipt.default_account_name} (default)</option>
            {accounts
              .filter((account) => account.name !== receipt.default_account_name)
              .map((account) => (
                <option key={account.id} value={account.id}>
                  {account.name}
                </option>
              ))}
          </select>
          <p className="mt-1 text-xs text-ink-faint">
            Choose where this purchase belongs. With nothing selected it goes to the
            clearly-labelled {receipt.default_account_name} account rather than a bank
            account.
          </p>
        </div>

        <div>
          <label htmlFor="receipt-category" className="label">
            Category
          </label>
          <select
            id="receipt-category"
            value={draft.categoryId}
            onChange={(event) => onChange({ categoryId: event.target.value })}
            className="input"
          >
            <option value="">Uncategorized</option>
            {categories.map((category) => (
              <option key={category.id} value={category.id}>
                {category.name}
              </option>
            ))}
          </select>
        </div>
      </div>

      {Object.keys(receipt.parse_notes).length > 0 ? (
        <details className="rounded-lg border border-line p-3">
          <summary className="cursor-pointer text-xs font-medium text-ink">
            How these fields were read
          </summary>
          <dl className="mt-2 space-y-1.5">
            {Object.entries(receipt.parse_notes).map(([field, note]) => (
              <div key={field} className="grid grid-cols-[80px_1fr] gap-2">
                <dt className="text-xs text-ink-faint">{field}</dt>
                <dd className="text-xs leading-relaxed text-ink-muted">{note}</dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}

      <details className="rounded-lg border border-line p-3">
        <summary className="cursor-pointer text-xs font-medium text-ink">
          Raw OCR text{' '}
          <Badge tone="neutral">
            {Math.round(receipt.ocr_confidence * 100)}% confidence
          </Badge>
        </summary>
        <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap rounded bg-surface-sunken p-2.5 font-mono text-[11px] leading-relaxed text-ink-muted">
          {receipt.raw_text || 'No text was recognized.'}
        </pre>
      </details>
    </div>
  );
}
