import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DangerZone } from '@/components/settings/danger-zone';
import type { DeletionResult } from '@/lib/types';

const mockApi = vi.hoisted(() => ({
  deleteData: vi.fn(),
  deleteAccount: vi.fn(),
}));
const mockSignOut = vi.hoisted(() => vi.fn());

vi.mock('@/lib/api-client', () => ({
  api: mockApi,
  clearTokenCache: vi.fn(),
  API_URL: 'http://localhost:8000',
}));
vi.mock('next-auth/react', () => ({ signOut: mockSignOut }));

function preview(overrides: Partial<DeletionResult> = {}): DeletionResult {
  return {
    dry_run: true,
    account_removed: false,
    total_rows: 712,
    rows_by_table: { transactions: 707, receipts: 4, alerts: 27, uploads: 0 },
    storage_objects_removed: 0,
    cache_keys_removed: 0,
    queued_jobs_cancelled: 2,
    errors: [],
    message: 'Dry run — nothing was removed.',
    ...overrides,
  };
}

function renderZone() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <DangerZone />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.deleteData.mockResolvedValue(preview());
  mockApi.deleteAccount.mockResolvedValue(preview({ account_removed: true }));
});

describe('DangerZone', () => {
  it('offers both scopes and starts with neither selected', () => {
    renderZone();
    expect(screen.getByRole('button', { name: 'Delete my data' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Delete my account' })).toBeInTheDocument();
    expect(screen.queryByLabelText(/Type DELETE/)).not.toBeInTheDocument();
  });

  it('previews the damage with a dry run before anything is destroyed', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my data' }));

    await waitFor(() => expect(mockApi.deleteData).toHaveBeenCalledWith(true));
    const shown = await screen.findByTestId('deletion-preview');
    expect(shown).toHaveTextContent('707');
    expect(shown).toHaveTextContent('transactions');
  });

  it('names the queued jobs it will cancel', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my data' }));

    const shown = await screen.findByTestId('deletion-preview');
    expect(shown).toHaveTextContent('2 cancelled');
  });

  it('says that stored files and cached analyses go too', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my data' }));

    expect(
      await screen.findByText(/Stored receipt files and every cached analysis are removed/),
    ).toBeInTheDocument();
  });

  it('refuses to delete until DELETE is typed exactly', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my data' }));
    const submit = await screen.findByRole('button', { name: 'Delete all my data' });
    expect(submit).toBeDisabled();

    await user.type(screen.getByLabelText(/Type DELETE/), 'delete');
    expect(submit).toBeDisabled();

    await user.clear(screen.getByLabelText(/Type DELETE/));
    await user.type(screen.getByLabelText(/Type DELETE/), 'DELETE');
    expect(submit).toBeEnabled();
  });

  it('performs the real deletion only after confirmation', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my data' }));
    await user.type(await screen.findByLabelText(/Type DELETE/), 'DELETE');
    await user.click(screen.getByRole('button', { name: 'Delete all my data' }));

    await waitFor(() => expect(mockApi.deleteData).toHaveBeenCalledWith(false));
  });

  it('signs the user out after the account is deleted', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my account' }));
    await user.type(await screen.findByLabelText(/Type DELETE/), 'DELETE');
    await user.click(screen.getByRole('button', { name: 'Delete my account' }));

    await waitFor(() =>
      expect(mockSignOut).toHaveBeenCalledWith({ callbackUrl: '/sign-in' }),
    );
  });

  it('warns that deleting the account cannot be undone', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my account' }));

    expect(
      await screen.findByText(/You will be signed out and cannot sign back in/),
    ).toBeInTheDocument();
  });

  it('cancelling abandons the flow without deleting', async () => {
    const user = userEvent.setup();
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my data' }));
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));

    await waitFor(() => expect(screen.queryByLabelText(/Type DELETE/)).not.toBeInTheDocument());
    expect(mockApi.deleteData).toHaveBeenCalledTimes(1); // the dry run only
    expect(mockApi.deleteData).not.toHaveBeenCalledWith(false);
  });

  it('surfaces a failure instead of pretending it worked', async () => {
    const user = userEvent.setup();
    mockApi.deleteData.mockResolvedValueOnce(preview());
    mockApi.deleteData.mockRejectedValueOnce(new Error('Database is unavailable'));
    renderZone();

    await user.click(screen.getByRole('button', { name: 'Delete my data' }));
    await user.type(await screen.findByLabelText(/Type DELETE/), 'DELETE');
    await user.click(screen.getByRole('button', { name: 'Delete all my data' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Database is unavailable');
  });
});
