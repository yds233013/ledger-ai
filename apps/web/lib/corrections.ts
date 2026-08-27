/**
 * Optimistic-update helpers for manual corrections.
 *
 * Kept as pure functions so the bulk-update and rollback behaviour can be
 * tested without mounting a page or a query client.
 */
import type { Category, CorrectionImpact, Page, Transaction } from './types';

/**
 * Which rows a correction will change.
 *
 * When the correction is retroactive we use the ids the server already told us
 * it would touch (from the impact preview), rather than re-deriving them on the
 * client — so the optimistic update and the write agree about protected rows.
 */
export function affectedIds(
  transactionId: string,
  applyToMatching: boolean,
  impact: CorrectionImpact | null | undefined,
): Set<string> {
  const ids = new Set<string>([transactionId]);
  if (applyToMatching) {
    for (const id of impact?.affected_ids ?? []) ids.add(id);
  }
  return ids;
}

/** Apply a category correction to every affected row of a cached page. */
export function applyOptimisticCorrection(
  page: Page<Transaction>,
  ids: Set<string>,
  category: Category,
): Page<Transaction> {
  return {
    ...page,
    items: page.items.map((item) =>
      ids.has(item.id)
        ? {
            ...item,
            category,
            confidence: 1,
            categorized_by: 'correction',
            is_corrected: true,
            needs_review: false,
          }
        : item,
    ),
  };
}
