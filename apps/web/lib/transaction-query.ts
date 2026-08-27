import type { TransactionQuery } from './api-client';

export const PAGE_SIZE = 50;

export const DEFAULT_QUERY: TransactionQuery = {
  limit: PAGE_SIZE,
  offset: 0,
  sort: 'date',
  order: 'desc',
};

const REVIEW_VALUES = ['needs_review', 'corrected', 'reviewed'] as const;
const SORT_VALUES = ['date', 'amount', 'merchant', 'confidence'] as const;

/**
 * Read the transaction filter state out of the URL.
 *
 * The URL is the single source of truth for filters, which is what makes
 * "view all flagged transactions" work as an ordinary link: the dashboard
 * navigates to `?flagged=1` and this page comes up already filtered, with a
 * shareable address and a working back button.
 *
 * Unknown or malformed values are dropped rather than passed through, so a
 * hand-edited URL cannot produce a request the API will reject.
 */
export function queryFromParams(params: URLSearchParams): TransactionQuery {
  const query: TransactionQuery = { ...DEFAULT_QUERY };

  const search = params.get('search');
  if (search) query.search = search;

  const category = params.get('category_slug');
  if (category) query.category_slug = category;

  const account = params.get('account_id');
  if (account) query.account_id = account;

  const merchant = params.get('merchant');
  if (merchant) query.merchant = merchant;

  const review = params.get('review');
  if (review && (REVIEW_VALUES as readonly string[]).includes(review)) {
    query.review = review as TransactionQuery['review'];
  }

  // Accepts the forms a link or a hand-typed URL might plausibly use.
  const flagged = params.get('flagged');
  if (flagged === '1' || flagged === 'true') query.flagged = true;

  const start = params.get('start_date');
  if (start) query.start_date = start;

  const end = params.get('end_date');
  if (end) query.end_date = end;

  const sort = params.get('sort');
  if (sort && (SORT_VALUES as readonly string[]).includes(sort)) {
    query.sort = sort as TransactionQuery['sort'];
  }

  const order = params.get('order');
  if (order === 'asc' || order === 'desc') query.order = order;

  const offset = Number.parseInt(params.get('offset') ?? '', 10);
  if (Number.isFinite(offset) && offset > 0) query.offset = offset;

  return query;
}

/**
 * Render filter state back into a query string.
 *
 * Defaults are omitted so a cleared page has a clean URL rather than a trail
 * of `sort=date&order=desc&offset=0`.
 */
export function paramsFromQuery(query: TransactionQuery): string {
  const params = new URLSearchParams();

  if (query.search) params.set('search', query.search);
  if (query.category_slug) params.set('category_slug', query.category_slug);
  if (query.account_id) params.set('account_id', query.account_id);
  if (query.merchant) params.set('merchant', query.merchant);
  if (query.review) params.set('review', query.review);
  if (query.flagged) params.set('flagged', '1');
  if (query.start_date) params.set('start_date', query.start_date);
  if (query.end_date) params.set('end_date', query.end_date);
  if (query.sort && query.sort !== 'date') params.set('sort', query.sort);
  if (query.order && query.order !== 'desc') params.set('order', query.order);
  if (query.offset) params.set('offset', String(query.offset));

  return params.toString();
}

/** How many filters are narrowing the list, for the "Clear N filters" control. */
export function activeFilterCount(query: TransactionQuery): number {
  return [
    query.search,
    query.category_slug,
    query.account_id,
    query.merchant,
    query.review,
    query.flagged,
    query.start_date,
    query.end_date,
  ].filter(Boolean).length;
}
