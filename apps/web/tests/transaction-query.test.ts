/**
 * The transactions page keeps its filter state in the URL. That is what makes
 * the dashboard's "View all flagged transactions" an ordinary link, and what
 * makes a filtered view shareable and back-button-able.
 */
import { describe, expect, it } from 'vitest';

import {
  activeFilterCount,
  DEFAULT_QUERY,
  paramsFromQuery,
  queryFromParams,
} from '@/lib/transaction-query';

function parse(search: string) {
  return queryFromParams(new URLSearchParams(search));
}

describe('queryFromParams', () => {
  it('defaults to an unfiltered first page', () => {
    expect(parse('')).toEqual(DEFAULT_QUERY);
  });

  it('reads the alert filter the dashboard links to', () => {
    expect(parse('flagged=1').flagged).toBe(true);
  });

  it('also accepts flagged=true', () => {
    expect(parse('flagged=true').flagged).toBe(true);
  });

  it('leaves flagged unset when absent', () => {
    expect(parse('search=coffee').flagged).toBeUndefined();
  });

  it('ignores a flagged value it does not recognise', () => {
    // A hand-edited URL must not produce a request the API would reject.
    expect(parse('flagged=maybe').flagged).toBeUndefined();
  });

  it('does not conflate the alert filter with the review queue', () => {
    const query = parse('flagged=1');
    expect(query.flagged).toBe(true);
    expect(query.review).toBeUndefined();
  });

  it('reads the review filter independently', () => {
    const query = parse('review=needs_review');
    expect(query.review).toBe('needs_review');
    expect(query.flagged).toBeUndefined();
  });

  it('drops an unknown review value', () => {
    expect(parse('review=nonsense').review).toBeUndefined();
  });

  it('reads search, category, account and merchant', () => {
    const query = parse(
      'search=coffee&category_slug=dining&account_id=acct-1&merchant=Sweetgreen',
    );
    expect(query).toMatchObject({
      search: 'coffee',
      category_slug: 'dining',
      account_id: 'acct-1',
      merchant: 'Sweetgreen',
    });
  });

  it('reads a date range', () => {
    expect(parse('start_date=2026-01-01&end_date=2026-03-31')).toMatchObject({
      start_date: '2026-01-01',
      end_date: '2026-03-31',
    });
  });

  it('reads a positive offset and ignores a nonsensical one', () => {
    expect(parse('offset=100').offset).toBe(100);
    expect(parse('offset=-5').offset).toBe(0);
    expect(parse('offset=abc').offset).toBe(0);
  });

  it('drops an unknown sort or order', () => {
    const query = parse('sort=colour&order=sideways');
    expect(query.sort).toBe('date');
    expect(query.order).toBe('desc');
  });
});

describe('paramsFromQuery', () => {
  it('writes nothing for a default query', () => {
    expect(paramsFromQuery(DEFAULT_QUERY)).toBe('');
  });

  it('writes the alert filter in the form the link uses', () => {
    expect(paramsFromQuery({ ...DEFAULT_QUERY, flagged: true })).toBe('flagged=1');
  });

  it('omits the alert filter once cleared', () => {
    expect(paramsFromQuery({ ...DEFAULT_QUERY, flagged: undefined })).toBe('');
  });

  it('round-trips a filter set', () => {
    const original = {
      ...DEFAULT_QUERY,
      flagged: true,
      search: 'coffee',
      category_slug: 'dining',
      offset: 50,
    };
    expect(queryFromParams(new URLSearchParams(paramsFromQuery(original)))).toEqual(original);
  });

  it('preserves other filters when the alert filter is cleared', () => {
    const withBoth = { ...DEFAULT_QUERY, flagged: true, search: 'coffee' };
    const cleared = queryFromParams(
      new URLSearchParams(paramsFromQuery({ ...withBoth, flagged: undefined })),
    );
    expect(cleared.search).toBe('coffee');
    expect(cleared.flagged).toBeUndefined();
  });
});

describe('activeFilterCount', () => {
  it('counts nothing for a default query', () => {
    expect(activeFilterCount(DEFAULT_QUERY)).toBe(0);
  });

  it('counts the alert filter', () => {
    expect(activeFilterCount({ ...DEFAULT_QUERY, flagged: true })).toBe(1);
  });

  it('counts the alert filter and the review filter separately', () => {
    expect(
      activeFilterCount({ ...DEFAULT_QUERY, flagged: true, review: 'needs_review' }),
    ).toBe(2);
  });

  it('does not count pagination or sorting as filters', () => {
    expect(
      activeFilterCount({ ...DEFAULT_QUERY, offset: 100, sort: 'amount', order: 'asc' }),
    ).toBe(0);
  });
});
