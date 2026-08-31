import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { UsageCard } from '@/components/settings/usage-card';

const mockApi = vi.hoisted(() => ({ usage: vi.fn() }));
vi.mock('@/lib/api-client', () => ({ api: mockApi, API_URL: 'http://localhost:8000' }));

const MB = 1024 * 1024;

const USAGE = {
  applies: true,
  resets_at: '2026-08-31T00:00:00+00:00',
  uploads_today: 3,
  uploads_per_day: 25,
  bytes_today: 5 * MB,
  upload_bytes_per_day: 50 * MB,
  stored_bytes: 20 * MB,
  stored_bytes_limit: 250 * MB,
  transaction_rows: 1200,
  transaction_rows_limit: 25000,
  receipts: 8,
  receipts_limit: 500,
  jobs_in_flight: 0,
  concurrent_jobs_limit: 3,
  max_upload_bytes: 10 * MB,
};

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <UsageCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('UsageCard', () => {
  it('shows usage against each budget', async () => {
    mockApi.usage.mockResolvedValue(USAGE);
    renderCard();

    expect(await screen.findByText('3 / 25')).toBeInTheDocument();
    expect(screen.getByText('1,200 / 25,000')).toBeInTheDocument();
    expect(screen.getByText('8 / 500')).toBeInTheDocument();
  });

  it('says the reset is UTC', async () => {
    mockApi.usage.mockResolvedValue(USAGE);
    renderCard();

    expect(await screen.findByText(/reset at midnight UTC/i)).toBeInTheDocument();
  });

  it('says that deleting files does not return the day’s upload count', async () => {
    // The counter is a record of what was sent today. Refunding it would make
    // the daily limit bypassable by uploading, deleting and uploading again.
    mockApi.usage.mockResolvedValue(USAGE);
    renderCard();

    expect(await screen.findByText(/does not return a day/i)).toBeInTheDocument();
  });

  it('presents the limits as a beta constraint rather than a plan', async () => {
    mockApi.usage.mockResolvedValue(USAGE);
    renderCard();

    expect(await screen.findByText(/They will change/)).toBeInTheDocument();
  });

  it('is absent for a demo account', async () => {
    mockApi.usage.mockResolvedValue({ ...USAGE, applies: false });
    renderCard();

    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText('Usage')).not.toBeInTheDocument();
  });

  it('names no account', async () => {
    mockApi.usage.mockResolvedValue(USAGE);
    renderCard();

    await screen.findByText('3 / 25');
    expect(document.body.textContent).not.toMatch(/@/);
  });
});
