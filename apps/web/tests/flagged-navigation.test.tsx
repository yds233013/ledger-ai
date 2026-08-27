/**
 * Arriving at Transactions from the dashboard's alert link.
 *
 * The page must request the alert filter (not the review queue), say plainly
 * that the list is filtered, and offer a way back to everything.
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { Category, Facets, Page, Transaction } from '@/lib/types';

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

const mockApi = vi.hoisted(() => ({
  transactions: vi.fn(),
  facets: vi.fn(),
  correctionImpact: vi.fn(),
  updateTransaction: vi.fn(),
}));

vi.mock('@/lib/api-client', () => ({
  api: mockApi,
  API_URL: 'http://localhost:8000',
}));

const dining: Category = {
  id: 'cat-dining', name: 'Dining & Restaurants', slug: 'dining', color: '#f59e0b', icon: 'fork',
};

function tx(id: string, overrides: Partial<Transaction> = {}): Transaction {
  return {
    id,
    posted_date: '2026-07-06',
    amount_cents: -1850,
    amount: -18.5,
    currency: 'USD',
    merchant: 'Sweetgreen',
    merchant_key: 'sweetgreen',
    raw_description: 'TST* SWEETGREEN [SYNTHETIC]',
    category: dining,
    // Deliberately confident and NOT in the review queue: this is exactly the
    // row the old ?review=needs_review link could never show.
    confidence: 1,
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

const facets: Facets = {
  categories: [dining],
  accounts: [
    {
      id: 'acct-1', name: 'SANDBOX — Everyday Checking', institution: 'Sandbox',
      account_type: 'checking', mask: '0001',
    },
  ],
  merchants: ['Sweetgreen'],
  review_count: 3,
  flagged_count: 2,
  total_count: 240,
};

function listing(items: Transaction[], total: number): Page<Transaction> {
  return { items, total, limit: 50, offset: 0, has_more: false };
}

async function renderPage() {
  const { default: TransactionsPage } = await import('@/app/(app)/transactions/page');
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <TransactionsPage />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  nav.search = '';
  mockApi.facets.mockResolvedValue(facets);
  mockApi.transactions.mockImplementation((query: { flagged?: boolean }) =>
    Promise.resolve(
      query?.flagged
        ? listing([tx('flagged-1'), tx('flagged-2')], 2)
        : listing([tx('a'), tx('b'), tx('c')], 240),
    ),
  );
});

describe('arriving with ?flagged=1', () => {
  it('asks the API for flagged transactions', async () => {
    nav.search = 'flagged=1';
    await renderPage();

    await waitFor(() => expect(mockApi.transactions).toHaveBeenCalled());
    expect(mockApi.transactions).toHaveBeenCalledWith(
      expect.objectContaining({ flagged: true }),
    );
  });

  it('never sends the review filter instead', async () => {
    nav.search = 'flagged=1';
    await renderPage();

    await waitFor(() => expect(mockApi.transactions).toHaveBeenCalled());
    const [query] = mockApi.transactions.mock.calls[0];
    expect(query.review).toBeUndefined();
  });

  it('says that the list is filtered', async () => {
    nav.search = 'flagged=1';
    await renderPage();

    const banner = await screen.findByTestId('flagged-filter-banner');
    expect(banner).toHaveTextContent(/Showing flagged transactions only/i);
  });

  it('does not claim the alerts are fraud', async () => {
    nav.search = 'flagged=1';
    await renderPage();

    const banner = await screen.findByTestId('flagged-filter-banner');
    expect(banner).toHaveTextContent(/not fraud determinations/i);
  });

  it('shows the flagged rows even though they need no review', async () => {
    nav.search = 'flagged=1';
    await renderPage();

    await waitFor(() => expect(screen.getAllByText('Sweetgreen').length).toBeGreaterThan(0));
    expect(screen.getByText(/2 of 240 shown/)).toBeInTheDocument();
  });
});

describe('clearing the alert filter', () => {
  it('offers a control to clear it', async () => {
    nav.search = 'flagged=1';
    await renderPage();

    expect(
      await screen.findByRole('button', { name: /Clear alert filter/i }),
    ).toBeInTheDocument();
  });

  it('removes it from the URL when cleared', async () => {
    nav.search = 'flagged=1';
    await renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /Clear alert filter/i }));

    expect(nav.replace).toHaveBeenCalled();
    const [url] = nav.replace.mock.calls.at(-1) as [string];
    expect(url).not.toContain('flagged');
  });

  it('keeps other filters when the alert filter is cleared', async () => {
    nav.search = 'flagged=1&search=sweetgreen';
    await renderPage();

    await userEvent.click(await screen.findByRole('button', { name: /Clear alert filter/i }));

    const [url] = nav.replace.mock.calls.at(-1) as [string];
    expect(url).toContain('search=sweetgreen');
    expect(url).not.toContain('flagged');
  });
});

describe('without the alert filter', () => {
  it('shows no filter banner', async () => {
    await renderPage();
    await waitFor(() => expect(mockApi.transactions).toHaveBeenCalled());
    expect(screen.queryByTestId('flagged-filter-banner')).not.toBeInTheDocument();
  });

  it('does not send flagged to the API', async () => {
    await renderPage();
    await waitFor(() => expect(mockApi.transactions).toHaveBeenCalled());
    const [query] = mockApi.transactions.mock.calls[0];
    expect(query.flagged).toBeUndefined();
  });

  it('offers a flagged shortcut that turns the filter on', async () => {
    await renderPage();

    const shortcut = await screen.findByRole('button', { name: /2 flagged/i });
    await userEvent.click(shortcut);

    const [url] = nav.replace.mock.calls.at(-1) as [string];
    expect(url).toContain('flagged=1');
  });

  it('keeps the flagged and review shortcuts distinct', async () => {
    await renderPage();

    expect(await screen.findByRole('button', { name: /2 flagged/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /3 need review/i })).toBeInTheDocument();
  });
});
