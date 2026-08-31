import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ConsentCard } from '@/components/settings/consent-card';
import { ConsentGate } from '@/components/upload/consent-gate';

const mockApi = vi.hoisted(() => ({ consents: vi.fn(), acceptConsents: vi.fn() }));
vi.mock('@/lib/api-client', () => ({ api: mockApi, API_URL: 'http://localhost:8000' }));

const NOTHING_ACCEPTED = {
  required: { terms: '2026-08-01', privacy: '2026-08-01', upload: '2026-08-01' },
  accepted: {},
  missing: ['terms', 'privacy', 'upload'],
};

const ALL_ACCEPTED = {
  required: { terms: '2026-08-01', privacy: '2026-08-01', upload: '2026-08-01' },
  accepted: { terms: '2026-08-01', privacy: '2026-08-01', upload: '2026-08-01' },
  missing: [],
};

const VERSION_BUMPED = {
  required: { terms: '2026-09-01', privacy: '2026-08-01', upload: '2026-08-01' },
  accepted: { terms: '2026-08-01', privacy: '2026-08-01', upload: '2026-08-01' },
  missing: ['terms'],
};

function renderWith(ui: React.ReactNode) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockApi.acceptConsents.mockResolvedValue(ALL_ACCEPTED);
});

describe('ConsentGate', () => {
  it('hides the upload form until the documents are accepted', async () => {
    mockApi.consents.mockResolvedValue(NOTHING_ACCEPTED);
    renderWith(
      <ConsentGate>
        <div data-testid="upload-form">dropzone</div>
      </ConsentGate>,
    );

    await screen.findByTestId('consent-gate');
    expect(screen.queryByTestId('upload-form')).not.toBeInTheDocument();
  });

  it('shows the upload form once nothing is outstanding', async () => {
    mockApi.consents.mockResolvedValue(ALL_ACCEPTED);
    renderWith(
      <ConsentGate>
        <div data-testid="upload-form">dropzone</div>
      </ConsentGate>,
    );

    expect(await screen.findByTestId('upload-form')).toBeInTheDocument();
    expect(screen.queryByTestId('consent-gate')).not.toBeInTheDocument();
  });

  it('does not lock the page when the lookup fails', async () => {
    // The API is the gate. A failed lookup here must not be a second one.
    mockApi.consents.mockRejectedValue(new Error('offline'));
    renderWith(
      <ConsentGate>
        <div data-testid="upload-form">dropzone</div>
      </ConsentGate>,
    );

    expect(await screen.findByTestId('upload-form')).toBeInTheDocument();
  });

  it('presents each document separately and unchecked', async () => {
    mockApi.consents.mockResolvedValue(NOTHING_ACCEPTED);
    renderWith(
      <ConsentGate>
        <div>form</div>
      </ConsentGate>,
    );

    for (const type of ['terms', 'privacy', 'upload']) {
      const box = await screen.findByTestId(`consent-checkbox-${type}`);
      expect(box).not.toBeChecked();
    }
  });

  it('keeps the submit button disabled until every box is ticked', async () => {
    const user = userEvent.setup();
    mockApi.consents.mockResolvedValue(NOTHING_ACCEPTED);
    renderWith(
      <ConsentGate>
        <div>form</div>
      </ConsentGate>,
    );

    const submit = await screen.findByTestId('consent-submit');
    expect(submit).toBeDisabled();

    await user.click(screen.getByTestId('consent-checkbox-terms'));
    await user.click(screen.getByTestId('consent-checkbox-privacy'));
    expect(submit).toBeDisabled();

    await user.click(screen.getByTestId('consent-checkbox-upload'));
    expect(submit).toBeEnabled();
  });

  it('records an explicit acceptance of exactly what was outstanding', async () => {
    const user = userEvent.setup();
    mockApi.consents.mockResolvedValue(NOTHING_ACCEPTED);
    renderWith(
      <ConsentGate>
        <div>form</div>
      </ConsentGate>,
    );

    await user.click(await screen.findByTestId('consent-checkbox-terms'));
    await user.click(screen.getByTestId('consent-checkbox-privacy'));
    await user.click(screen.getByTestId('consent-checkbox-upload'));
    await user.click(screen.getByTestId('consent-submit'));

    await waitFor(() =>
      expect(mockApi.acceptConsents).toHaveBeenCalledWith(['terms', 'privacy', 'upload']),
    );
  });

  it('asks again only for the document whose version changed', async () => {
    mockApi.consents.mockResolvedValue(VERSION_BUMPED);
    renderWith(
      <ConsentGate>
        <div>form</div>
      </ConsentGate>,
    );

    expect(await screen.findByTestId('consent-checkbox-terms')).toBeInTheDocument();
    expect(screen.queryByTestId('consent-checkbox-privacy')).not.toBeInTheDocument();
    expect(screen.queryByTestId('consent-checkbox-upload')).not.toBeInTheDocument();
  });

  it('says a re-prompt is a change, not a discarded answer', async () => {
    mockApi.consents.mockResolvedValue(VERSION_BUMPED);
    renderWith(
      <ConsentGate>
        <div>form</div>
      </ConsentGate>,
    );

    expect(await screen.findByText(/have changed/i)).toBeInTheDocument();
    expect(screen.getByText(/You accepted v2026-08-01 of this document/)).toBeInTheDocument();
  });

  it('shows the version being accepted next to each document', async () => {
    mockApi.consents.mockResolvedValue(NOTHING_ACCEPTED);
    renderWith(
      <ConsentGate>
        <div>form</div>
      </ConsentGate>,
    );

    expect(await screen.findAllByText('v2026-08-01')).toHaveLength(3);
  });
});

describe('ConsentCard', () => {
  it('shows the accepted version of each document', async () => {
    mockApi.consents.mockResolvedValue(ALL_ACCEPTED);
    renderWith(<ConsentCard isDemo={false} />);

    expect(await screen.findAllByText('Accepted v2026-08-01')).toHaveLength(3);
    expect(screen.getByText('Up to date')).toBeInTheDocument();
  });

  it('flags an out-of-date acceptance without calling it missing', async () => {
    mockApi.consents.mockResolvedValue(VERSION_BUMPED);
    renderWith(<ConsentCard isDemo={false} />);

    expect(await screen.findByText('v2026-08-01 — out of date')).toBeInTheDocument();
    expect(screen.getByText('1 outstanding')).toBeInTheDocument();
  });

  it('says that only uploading is gated', async () => {
    mockApi.consents.mockResolvedValue(NOTHING_ACCEPTED);
    renderWith(<ConsentCard isDemo={false} />);

    expect(
      await screen.findByText(/reading, exporting and deleting your own data — is never gated/i),
    ).toBeInTheDocument();
  });

  it('is absent for a demo account', () => {
    renderWith(<ConsentCard isDemo />);
    expect(screen.queryByText('Agreements')).not.toBeInTheDocument();
    expect(mockApi.consents).not.toHaveBeenCalled();
  });

  it('never claims the documents were reviewed by a lawyer', async () => {
    mockApi.consents.mockResolvedValue(NOTHING_ACCEPTED);
    renderWith(<ConsentCard isDemo={false} />);

    expect(
      await screen.findByText(/These terms have not been reviewed by a lawyer/),
    ).toBeInTheDocument();
  });
});
