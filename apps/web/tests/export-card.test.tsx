import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ExportCard } from '@/components/settings/export-card';

const mockApi = vi.hoisted(() => ({ exportData: vi.fn() }));
vi.mock('@/lib/api-client', () => ({ api: mockApi, API_URL: 'http://localhost:8000' }));

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ExportCard />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  // jsdom has no object-URL implementation.
  URL.createObjectURL = vi.fn(() => 'blob:mock');
  URL.revokeObjectURL = vi.fn();
});

describe('ExportCard', () => {
  it('describes what the export contains', () => {
    renderCard();
    expect(screen.getByText(/Transactions, receipts, alerts, corrections/)).toBeInTheDocument();
  });

  it('states that money is stored as integer cents', () => {
    renderCard();
    expect(screen.getByText(/integer cents and\s+never uses floating point/)).toBeInTheDocument();
  });

  it('downloads the archive through an object URL', async () => {
    const user = userEvent.setup();
    mockApi.exportData.mockResolvedValue({
      blob: new Blob(['zip'], { type: 'application/zip' }),
      filename: 'ledgerai-export-20260826.zip',
    });
    renderCard();

    await user.click(screen.getByRole('button', { name: 'Download export' }));

    await waitFor(() => expect(mockApi.exportData).toHaveBeenCalled());
    await waitFor(() => expect(URL.createObjectURL).toHaveBeenCalled());
    // The temporary URL must not be left dangling.
    await waitFor(() => expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:mock'));
  });

  it('reports a failure rather than silently doing nothing', async () => {
    const user = userEvent.setup();
    mockApi.exportData.mockRejectedValue(new Error('Export failed (500).'));
    renderCard();

    await user.click(screen.getByRole('button', { name: 'Download export' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Export failed (500).');
  });
});
