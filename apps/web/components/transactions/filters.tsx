'use client';

import { Badge } from '@/components/ui/primitives';
import type { TransactionQuery } from '@/lib/api-client';
import type { Facets } from '@/lib/types';

export function TransactionFilters({
  facets,
  query,
  onChange,
  onReset,
}: {
  facets: Facets;
  query: TransactionQuery;
  onChange: (patch: Partial<TransactionQuery>) => void;
  onReset: () => void;
}) {
  const activeCount = [
    query.search,
    query.category_slug,
    query.account_id,
    query.merchant,
    query.review,
    query.start_date,
    query.end_date,
  ].filter(Boolean).length;

  return (
    <div className="space-y-3 border-b border-line p-4">
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-[220px] flex-1">
          <label htmlFor="tx-search" className="label">
            Search
          </label>
          <input
            id="tx-search"
            type="search"
            placeholder="Merchant or description…"
            value={query.search ?? ''}
            onChange={(event) => onChange({ search: event.target.value || undefined })}
            className="input"
          />
        </div>

        <div>
          <label htmlFor="tx-category" className="label">
            Category
          </label>
          <select
            id="tx-category"
            value={query.category_slug ?? ''}
            onChange={(event) => onChange({ category_slug: event.target.value || undefined })}
            className="input w-44"
          >
            <option value="">All categories</option>
            {facets.categories.map((category) => (
              <option key={category.id} value={category.slug}>
                {category.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="tx-account" className="label">
            Account
          </label>
          <select
            id="tx-account"
            value={query.account_id ?? ''}
            onChange={(event) => onChange({ account_id: event.target.value || undefined })}
            className="input w-48"
          >
            <option value="">All accounts</option>
            {facets.accounts.map((account) => (
              <option key={account.id} value={account.id}>
                {account.name}
              </option>
            ))}
          </select>
        </div>

        <div>
          <label htmlFor="tx-review" className="label">
            Status
          </label>
          <select
            id="tx-review"
            value={query.review ?? ''}
            onChange={(event) =>
              onChange({
                review: (event.target.value || undefined) as TransactionQuery['review'],
              })
            }
            className="input w-40"
          >
            <option value="">All</option>
            <option value="needs_review">Needs review</option>
            <option value="corrected">Edited by me</option>
            <option value="reviewed">Confirmed</option>
          </select>
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <div>
          <label htmlFor="tx-start" className="label">
            From
          </label>
          <input
            id="tx-start"
            type="date"
            value={query.start_date ?? ''}
            onChange={(event) => onChange({ start_date: event.target.value || undefined })}
            className="input w-40"
          />
        </div>
        <div>
          <label htmlFor="tx-end" className="label">
            To
          </label>
          <input
            id="tx-end"
            type="date"
            value={query.end_date ?? ''}
            onChange={(event) => onChange({ end_date: event.target.value || undefined })}
            className="input w-40"
          />
        </div>
        <div>
          <label htmlFor="tx-merchant" className="label">
            Merchant
          </label>
          <select
            id="tx-merchant"
            value={query.merchant ?? ''}
            onChange={(event) => onChange({ merchant: event.target.value || undefined })}
            className="input w-52"
          >
            <option value="">All merchants</option>
            {facets.merchants.map((merchant) => (
              <option key={merchant} value={merchant}>
                {merchant}
              </option>
            ))}
          </select>
        </div>

        <div className="ml-auto flex items-center gap-3 pb-0.5">
          {facets.review_count > 0 ? (
            <button
              type="button"
              onClick={() => onChange({ review: 'needs_review' })}
              className="text-xs font-medium text-caution hover:underline"
            >
              <Badge tone="caution">{facets.review_count} need review</Badge>
            </button>
          ) : null}
          {activeCount > 0 ? (
            <button type="button" onClick={onReset} className="btn-ghost text-xs">
              Clear {activeCount} filter{activeCount === 1 ? '' : 's'}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
