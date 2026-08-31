import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ReviewTable } from '@/components/statements/review-table';
import { KindPicker } from '@/components/upload/kind-picker';
import type { StatementRow } from '@/lib/types';

const mockApi = vi.hoisted(() => ({ updateStatementRow: vi.fn() }));
vi.mock('@/lib/api-client', () => ({ api: mockApi, API_URL: 'http://localhost:8000' }));

function row(over: Partial<StatementRow> = {}): StatementRow {
  return {
    id: 'row-1',
    source_page: 0,
    posted_date: '2026-08-12',
    description: 'SANDBOX GROCERS 0042',
    amount_cents: -4210,
    balance_cents: 190455,
    direction: 'debit',
    confidence: 1,
    flags: [],
    excluded: false,
    edited: false,
    duplicate_of_existing: false,
    ...over,
  };
}

function renderTable(rows: StatementRow[], editable = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <ReviewTable importId="imp-1" rows={rows} editable={editable} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('ReviewTable', () => {
  it('shows the direction in words rather than a sign', () => {
    renderTable([row(), row({ id: 'row-2', direction: 'credit', amount_cents: 180000 })]);
    expect(screen.getByText('Money out')).toBeInTheDocument();
    expect(screen.getByText('Money in')).toBeInTheDocument();
  });

  it('marks a low-confidence row for attention', () => {
    renderTable([row({ confidence: 0.4 })]);
    expect(screen.getByText('Check this')).toBeInTheDocument();
  });

  it('explains a flag instead of printing its token', () => {
    renderTable([row({ confidence: 0.4, flags: ['balance_mismatch'] })]);
    expect(screen.getByText(/running balance does not move by this amount/i)).toBeInTheDocument();
    expect(screen.queryByText('balance_mismatch')).not.toBeInTheDocument();
  });

  it('explains an unresolved sign as a question the reader can answer', () => {
    renderTable([row({ confidence: 0.5, flags: ['sign_unresolved'] })]);
    expect(screen.getByText(/whether it is money in or out/i)).toBeInTheDocument();
  });

  it('marks a row already present in the ledger', () => {
    renderTable([row({ duplicate_of_existing: true })]);
    expect(screen.getByText('Already imported')).toBeInTheDocument();
  });

  it('excludes a row when its checkbox is cleared', async () => {
    const user = userEvent.setup();
    mockApi.updateStatementRow.mockResolvedValue(row({ excluded: true }));
    renderTable([row()]);

    await user.click(screen.getByTestId('include-row-1'));

    await waitFor(() =>
      expect(mockApi.updateStatementRow).toHaveBeenCalledWith('imp-1', 'row-1', {
        excluded: true,
      }),
    );
  });

  it('is read-only once the import is committed', () => {
    renderTable([row()], false);
    expect(screen.getByTestId('include-row-1')).toBeDisabled();
  });

  it('credits a row the user corrected rather than the parser', () => {
    renderTable([row({ edited: true, confidence: 1 })]);
    expect(screen.getByText('You corrected this')).toBeInTheDocument();
  });
});

describe('KindPicker', () => {
  it('asks what a PDF is instead of guessing', () => {
    render(<KindPicker value="statement" onChange={() => {}} />);
    expect(screen.getByText(/If you upload a PDF, what is it\?/i)).toBeInTheDocument();
    expect(screen.getByTestId('kind-statement')).toBeChecked();
    expect(screen.getByTestId('kind-receipt')).not.toBeChecked();
  });

  it('reports the choice', async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<KindPicker value="statement" onChange={onChange} />);

    await user.click(screen.getByTestId('kind-receipt'));
    expect(onChange).toHaveBeenCalledWith('receipt');
  });

  it('says scanned statements are refused and points at CSV', () => {
    render(<KindPicker value="statement" onChange={() => {}} />);
    expect(screen.getByText(/scanned statement is refused/i)).toBeInTheDocument();
    expect(screen.getByText(/CSV export/i)).toBeInTheDocument();
  });

  it('says the original PDF is deleted after import', () => {
    render(<KindPicker value="statement" onChange={() => {}} />);
    expect(screen.getByText(/deleted as soon as you confirm/i)).toBeInTheDocument();
  });
});
