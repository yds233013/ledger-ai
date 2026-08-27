import { describe, expect, it } from 'vitest';

import { affectedIds, applyOptimisticCorrection } from '@/lib/corrections';
import type { Category, CorrectionImpact, Page, Transaction } from '@/lib/types';

const dining: Category = {
  id: 'cat-dining', name: 'Dining & Restaurants', slug: 'dining', color: '#f59e0b', icon: 'fork',
};
const travel: Category = {
  id: 'cat-travel', name: 'Travel', slug: 'travel', color: '#0ea5e9', icon: 'plane',
};

function tx(id: string, overrides: Partial<Transaction> = {}): Transaction {
  return {
    id,
    posted_date: '2026-07-06',
    amount_cents: -2500,
    amount: -25,
    currency: 'USD',
    merchant: 'Sweetgreen',
    merchant_key: 'sweetgreen',
    raw_description: 'SWEETGREEN',
    category: dining,
    confidence: 0.9,
    categorized_by: 'rule',
    needs_review: false,
    is_corrected: false,
    account_id: 'acct-1',
    account_name: 'SANDBOX — Everyday Checking',
    upload_id: null,
    created_at: '2026-07-06T00:00:00Z',
    ...overrides,
  };
}

function page(): Page<Transaction> {
  return {
    items: [tx('a'), tx('b'), tx('c'), tx('other', { merchant: 'Uber', merchant_key: 'uber' })],
    total: 4,
    limit: 50,
    offset: 0,
    has_more: false,
  };
}

const impact: CorrectionImpact = {
  merchant: 'Sweetgreen',
  merchant_key: 'sweetgreen',
  matching_count: 2,
  affected_count: 2,
  protected_count: 0,
  already_correct_count: 0,
  affected_ids: ['b', 'c'],
};

describe('affectedIds', () => {
  it('touches only the edited row when retroactive is off', () => {
    expect([...affectedIds('a', false, impact)]).toEqual(['a']);
  });

  it('includes the server-reported siblings when retroactive is on', () => {
    expect([...affectedIds('a', true, impact)].sort()).toEqual(['a', 'b', 'c']);
  });

  it('trusts the server about protected rows rather than re-deriving them', () => {
    // 'c' was individually corrected, so the server excluded it. The client
    // must not add it back by matching on merchant.
    const protectedImpact = { ...impact, affected_ids: ['b'], protected_count: 1, affected_count: 1 };
    expect([...affectedIds('a', true, protectedImpact)].sort()).toEqual(['a', 'b']);
  });

  it('degrades safely when the impact has not loaded', () => {
    expect([...affectedIds('a', true, null)]).toEqual(['a']);
  });
});

describe('applyOptimisticCorrection', () => {
  it('updates every affected row and leaves the rest alone', () => {
    const result = applyOptimisticCorrection(page(), affectedIds('a', true, impact), travel);

    expect(result.items.filter((i) => i.category?.slug === 'travel').map((i) => i.id)).toEqual([
      'a', 'b', 'c',
    ]);
    expect(result.items.find((i) => i.id === 'other')?.category?.slug).toBe('dining');
  });

  it('marks corrected rows as confirmed', () => {
    const result = applyOptimisticCorrection(page(), new Set(['a']), travel);
    const updated = result.items.find((i) => i.id === 'a')!;

    expect(updated.is_corrected).toBe(true);
    expect(updated.confidence).toBe(1);
    expect(updated.categorized_by).toBe('correction');
    expect(updated.needs_review).toBe(false);
  });

  it('does not mutate the original page — the snapshot stays intact for rollback', () => {
    const original = page();
    const snapshot = structuredClone(original);

    applyOptimisticCorrection(original, affectedIds('a', true, impact), travel);

    expect(original).toEqual(snapshot);
  });

  it('preserves pagination metadata', () => {
    const result = applyOptimisticCorrection(page(), new Set(['a']), travel);
    expect(result.total).toBe(4);
    expect(result.has_more).toBe(false);
  });
});
