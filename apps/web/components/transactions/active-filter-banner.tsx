'use client';

import type { TransactionQuery } from '@/lib/api-client';

/**
 * Says out loud that the list is filtered to alerted transactions.
 *
 * Arriving here from the dashboard's "View all flagged transactions" link
 * silently narrows the table, and a filtered list that does not announce
 * itself reads as a complete one — the user concludes they have far fewer
 * transactions than they do. The banner names the filter, explains what it
 * selects, and offers the way out.
 */
export function ActiveFilterBanner({
  query,
  onClearFlagged,
}: {
  query: TransactionQuery;
  onClearFlagged: () => void;
}) {
  if (!query.flagged) return null;

  return (
    <div
      role="status"
      data-testid="flagged-filter-banner"
      className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-caution/40 bg-caution/5 px-4 py-3"
    >
      <div>
        <p className="text-sm font-medium text-ink">Showing flagged transactions only</p>
        <p className="mt-0.5 text-xs leading-relaxed text-ink-muted">
          These carry an open alert — a possible duplicate, an unusual amount, or a
          first charge at a merchant. They are statistical observations about your own
          data, not fraud determinations.
        </p>
      </div>
      <button type="button" onClick={onClearFlagged} className="btn-secondary shrink-0">
        Clear alert filter
      </button>
    </div>
  );
}
