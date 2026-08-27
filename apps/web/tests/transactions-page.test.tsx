/**
 * Integration test for the correction flow, mounting the real page against a
 * mocked API. This is what proves requirement 8: the optimistic update must
 * roll back correctly — and completely — when the backend rejects the change.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Category, CorrectionImpact, Facets, Page, Transaction } from '@/lib/types';

/**
 * The page reads its filter state from the URL, so navigation is mocked with a
 * mutable search string that `replace` writes back into. That makes the URL an
 * observable part of the test rather than an invisible side effect.
 */
const nav = vi.hoisted(() => ({
  search: '',
  replace: vi.fn((url: string) => {
    nav.search = url.includes('?') ? url.slice(url.indexOf('?') + 1) : '';
  }),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ replace: nav.replace, push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => '/transactions',
  useSearchParams: () => new URLSearchParams(nav.search),
}));

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

const impact: CorrectionImpact = {
  merchant: 'Sweetgreen',
  merchant_key: 'sweetgreen',
  matching_count: 2,
  affected_count: 2,
  protected_count: 1,
  already_correct_count: 0,
  affected_ids: ['b', 'c'],
};

const facets: Facets = {
  categories: [dining, travel],
  accounts: [
    { id: 'acct-1', name: 'SANDBOX — Everyday Checking', institution: 'Sandbox', account_type: 'checking', mask: '0001' },
  ],
  merchants: ['Sweetgreen'],
  review_count: 0,
  flagged_count: 0,
  total_count: 4,
};

function listing(): Page<Transaction> {
  return {
    items: [tx('a'), tx('b'), tx('c'), tx('d', { merchant: 'Uber', merchant_key: 'uber' })],
    total: 4,
    limit: 50,
    offset: 0,
    has_more: false,
  };
}

const mockApi = {
  transactions: vi.fn(),
  facets: vi.fn(),
  correctionImpact: vi.fn(),
  updateTransaction: vi.fn(),
};

vi.mock('@/lib/api-client', () => ({
  api: mockApi,
  ApiError: class ApiError extends Error {
    constructor(message: string, public status: number) {
      super(message);
    }
  },
}));

const { default: TransactionsPage } = await import('@/app/(app)/transactions/page');

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TransactionsPage />
    </QueryClientProvider>,
  );
}

/** Categories of the three Sweetgreen rows, in order. */
function sweetgreenCategories(): string[] {
  return ['a', 'b', 'c'].map(
    (id) => (screen.getByLabelText(`Category for Sweetgreen`, { selector: `#category-${id}` }) as HTMLSelectElement).value,
  );
}

beforeEach(() => {
  nav.search = '';
  nav.replace.mockClear();
  vi.clearAllMocks();
  mockApi.transactions.mockResolvedValue(listing());
  mockApi.facets.mockResolvedValue(facets);
  mockApi.correctionImpact.mockResolvedValue(impact);
});

describe('Transactions page — correction flow', () => {
  it('does not write anything until the user confirms', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole('table');

    await user.selectOptions(document.querySelector('#category-a') as HTMLSelectElement, 'cat-travel');

    await screen.findByTestId('correction-confirm');
    expect(mockApi.updateTransaction).not.toHaveBeenCalled();
  });

  it('previews the impact and applies retroactively by default', async () => {
    const user = userEvent.setup();
    mockApi.updateTransaction.mockResolvedValue({
      transaction: tx('a', { category: travel, is_corrected: true }),
      applied_to_matching: true,
      impact,
    });

    renderPage();
    await screen.findByRole('table');
    await user.selectOptions(document.querySelector('#category-a') as HTMLSelectElement, 'cat-travel');

    const confirm = await screen.findByTestId('correction-confirm');
    await waitFor(() =>
      expect(within(confirm).getByText(/2 other transactions from Sweetgreen/)).toBeInTheDocument(),
    );
    expect(within(confirm).getByText(/1 transaction was edited individually/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Apply to 3 transactions' }));

    expect(mockApi.updateTransaction).toHaveBeenCalledWith('a', {
      category_id: 'cat-travel',
      apply_to_matching: true,
    });
  });

  it('optimistically updates every affected row, and only those', async () => {
    const user = userEvent.setup();
    let resolveUpdate: (value: unknown) => void = () => {};
    mockApi.updateTransaction.mockReturnValue(new Promise((resolve) => { resolveUpdate = resolve; }));

    renderPage();
    await screen.findByRole('table');
    await user.selectOptions(document.querySelector('#category-a') as HTMLSelectElement, 'cat-travel');
    await screen.findByTestId('correction-confirm');
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply to 3/ })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: /Apply to 3/ }));

    // Before the request resolves, a, b and c already show Travel; d does not.
    await waitFor(() => expect(sweetgreenCategories()).toEqual(['cat-travel', 'cat-travel', 'cat-travel']));
    expect((document.querySelector('#category-d') as HTMLSelectElement).value).toBe('cat-dining');

    // Settle the pending request inside act() so the resulting state update
    // does not land after the test has finished.
    await act(async () => {
      resolveUpdate({ transaction: tx('a', { category: travel }), applied_to_matching: true, impact });
    });
  });

  it('rolls the whole bulk change back when the backend fails', async () => {
    const user = userEvent.setup();
    mockApi.updateTransaction.mockRejectedValue(new Error('Database is unavailable'));

    renderPage();
    await screen.findByRole('table');
    await user.selectOptions(document.querySelector('#category-a') as HTMLSelectElement, 'cat-travel');
    await screen.findByTestId('correction-confirm');
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply to 3/ })).toBeEnabled());
    await user.click(screen.getByRole('button', { name: /Apply to 3/ }));

    // Every optimistically-changed row must return to its previous category —
    // a partial rollback would be worse than no optimism at all.
    await waitFor(() =>
      expect(sweetgreenCategories()).toEqual(['cat-dining', 'cat-dining', 'cat-dining']),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent('Database is unavailable');
  });

  it('applies to a single row when the user turns retroactive off', async () => {
    const user = userEvent.setup();
    let resolveUpdate: (value: unknown) => void = () => {};
    mockApi.updateTransaction.mockReturnValue(new Promise((resolve) => { resolveUpdate = resolve; }));

    renderPage();
    await screen.findByRole('table');
    await user.selectOptions(document.querySelector('#category-a') as HTMLSelectElement, 'cat-travel');
    await screen.findByTestId('correction-confirm');
    await waitFor(() => expect(screen.getByRole('button', { name: /Apply to 3/ })).toBeEnabled());

    await user.click(screen.getByRole('checkbox', { name: /Apply to all matching/ }));
    await user.click(screen.getByRole('button', { name: 'Apply to this one' }));

    expect(mockApi.updateTransaction).toHaveBeenCalledWith('a', {
      category_id: 'cat-travel',
      apply_to_matching: false,
    });
    // Only the edited row changes optimistically; the siblings are untouched.
    await waitFor(() => expect(sweetgreenCategories()[0]).toBe('cat-travel'));
    expect(sweetgreenCategories()[1]).toBe('cat-dining');
    expect(sweetgreenCategories()[2]).toBe('cat-dining');

    await act(async () => {
      resolveUpdate({
        transaction: tx('a', { category: travel }),
        applied_to_matching: false,
        impact: { ...impact, affected_count: 0, affected_ids: [] },
      });
    });
  });

  it('abandons the correction on cancel', async () => {
    const user = userEvent.setup();
    renderPage();
    await screen.findByRole('table');
    await user.selectOptions(document.querySelector('#category-a') as HTMLSelectElement, 'cat-travel');
    await screen.findByTestId('correction-confirm');

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByTestId('correction-confirm')).not.toBeInTheDocument());
    expect(mockApi.updateTransaction).not.toHaveBeenCalled();
    expect(sweetgreenCategories()).toEqual(['cat-dining', 'cat-dining', 'cat-dining']);
  });
});
