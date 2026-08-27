'use client';

import { useState } from 'react';

import { Badge, Spinner } from '@/components/ui/primitives';
import { cn } from '@/lib/cn';
import { formatShortDate, formatSpend } from '@/lib/format';
import type { Category, CorrectionImpact, Transaction } from '@/lib/types';

import { ConfidenceIndicator } from './confidence';

export interface PendingCorrection {
  categoryId: string;
  applyToMatching: boolean;
}

export function TransactionRow({
  transaction,
  categories,
  onCategoryChange,
  isSaving = false,
  pending = null,
  impact = null,
  impactLoading = false,
  onToggleApplyToMatching,
  onConfirm,
  onCancel,
}: {
  transaction: Transaction;
  categories: Category[];
  onCategoryChange: (transactionId: string, categoryId: string) => void;
  isSaving?: boolean;
  pending?: PendingCorrection | null;
  impact?: CorrectionImpact | null;
  impactLoading?: boolean;
  onToggleApplyToMatching?: (next: boolean) => void;
  onConfirm?: () => void;
  onCancel?: () => void;
}) {
  const [showRaw, setShowRaw] = useState(false);
  const pendingCategory = pending
    ? categories.find((item) => item.id === pending.categoryId)
    : undefined;

  return (
    <>
      <tr
        data-testid="transaction-row"
        className={cn(
          'border-b border-line transition-colors hover:bg-surface-sunken/60',
          transaction.needs_review && 'bg-caution/5',
          pending && 'bg-brand-soft/40',
        )}
      >
        <td className="whitespace-nowrap px-4 py-3 text-sm text-ink-muted tabular-nums">
          {formatShortDate(transaction.posted_date)}
        </td>

        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium text-ink">{transaction.merchant}</span>
            {transaction.is_corrected ? <Badge tone="brand">Edited</Badge> : null}
          </div>
          <button
            type="button"
            onClick={() => setShowRaw((open) => !open)}
            className="mt-0.5 max-w-md truncate text-left text-xs text-ink-faint hover:text-ink-muted"
            title={transaction.raw_description}
          >
            {showRaw ? transaction.raw_description : transaction.account_name}
          </button>
        </td>

        <td className="px-4 py-3">
          <label className="sr-only" htmlFor={`category-${transaction.id}`}>
            Category for {transaction.merchant}
          </label>
          <div className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="h-2 w-2 shrink-0 rounded-full"
              style={{
                backgroundColor:
                  pendingCategory?.color ?? transaction.category?.color ?? '#64748b',
              }}
            />
            <select
              id={`category-${transaction.id}`}
              value={pending?.categoryId ?? transaction.category?.id ?? ''}
              disabled={isSaving}
              onChange={(event) => onCategoryChange(transaction.id, event.target.value)}
              className="w-44 rounded-md border border-transparent bg-transparent px-1.5 py-1 text-sm text-ink hover:border-line focus:border-brand disabled:opacity-60"
            >
              {!transaction.category ? <option value="">Uncategorized</option> : null}
              {categories.map((category) => (
                <option key={category.id} value={category.id}>
                  {category.name}
                </option>
              ))}
            </select>
            {isSaving ? <Spinner className="h-3 w-3 text-ink-faint" /> : null}
          </div>
        </td>

        <td className="px-4 py-3">
          <div className="flex items-center gap-2">
            <ConfidenceIndicator
              confidence={transaction.confidence}
              source={transaction.categorized_by}
            />
            {transaction.needs_review ? <Badge tone="caution">Review</Badge> : null}
          </div>
        </td>

        <td
          className={cn(
            'whitespace-nowrap px-4 py-3 text-right text-sm font-medium tabular-nums',
            transaction.amount_cents > 0 ? 'text-positive' : 'text-ink',
          )}
        >
          {transaction.amount_cents > 0 ? '+' : '−'}
          {formatSpend(transaction.amount_cents)}
        </td>
      </tr>

      {pending ? (
        <tr data-testid="correction-confirm" className="border-b border-line bg-brand-soft/25">
          <td colSpan={5} className="px-4 py-3">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
              <p className="text-sm text-ink">
                Recategorize to{' '}
                <span className="font-medium">{pendingCategory?.name ?? 'this category'}</span>
              </p>

              <label className="flex items-center gap-2 text-sm text-ink">
                <input
                  type="checkbox"
                  checked={pending.applyToMatching}
                  onChange={(event) => onToggleApplyToMatching?.(event.target.checked)}
                  className="h-4 w-4 rounded border-line accent-[rgb(var(--brand))]"
                />
                <span>
                  Apply to all matching transactions
                  {impactLoading ? (
                    <span className="ml-1.5 text-ink-faint">(counting…)</span>
                  ) : impact ? (
                    <span className="ml-1.5 text-ink-muted">
                      ({impact.affected_count} other{' '}
                      {impact.affected_count === 1 ? 'transaction' : 'transactions'} from{' '}
                      {impact.merchant})
                    </span>
                  ) : null}
                </span>
              </label>

              <div className="ml-auto flex items-center gap-2">
                <button type="button" onClick={onCancel} className="btn-ghost">
                  Cancel
                </button>
                <button
                  type="button"
                  onClick={onConfirm}
                  disabled={isSaving || impactLoading}
                  className="btn-primary"
                >
                  {isSaving ? <Spinner /> : null}
                  {pending.applyToMatching && impact && impact.affected_count > 0
                    ? `Apply to ${impact.affected_count + 1} transactions`
                    : 'Apply to this one'}
                </button>
              </div>

              {pending.applyToMatching && impact && impact.protected_count > 0 ? (
                <p className="w-full text-xs text-ink-muted">
                  {impact.protected_count}{' '}
                  {impact.protected_count === 1 ? 'transaction was' : 'transactions were'} edited
                  individually before and will be left unchanged.
                </p>
              ) : null}

              {pending.applyToMatching &&
              impact &&
              impact.affected_count === 0 &&
              impact.matching_count > 0 ? (
                <p className="w-full text-xs text-ink-muted">
                  Every other {impact.merchant} transaction already has this category or was
                  edited individually.
                </p>
              ) : null}
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}
