import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { AlertsPanel } from '@/components/dashboard/alerts-panel';
import type { Alert } from '@/lib/types';

// vi.mock is hoisted, so the mock object must be created inside vi.hoisted.
const mockApi = vi.hoisted(() => ({ updateAlert: vi.fn() }));
vi.mock('@/lib/api-client', () => ({ api: mockApi, API_URL: 'http://localhost:8000' }));

const DISCLAIMER =
  'Alerts describe unusual patterns in your own uploaded data. They are not fraud detection and do not mean anything is wrong.';

function makeAlert(overrides: Partial<Alert> = {}): Alert {
  return {
    id: 'alert-1',
    alert_type: 'unusual_amount',
    severity: 'medium',
    severity_note: 'Unusual compared with your own history. Not necessarily a problem.',
    status: 'open',
    message:
      '$184.00 at Blue Bottle Coffee is much larger than your usual Dining spending, where the typical charge is about $8.20.',
    evidence: {
      rule: 'robust z score above 3.5 using median and median absolute deviation',
      sample_size: 42,
      median_cents: 820,
      mad_cents: 180,
      z_score: 19.85,
      disclaimer: DISCLAIMER,
    },
    created_at: '2026-08-20T00:00:00Z',
    transaction_id: 'tx-1',
    transaction_merchant: 'Blue Bottle Coffee',
    transaction_date: '2026-07-23',
    transaction_amount: -184,
    ...overrides,
  };
}

function renderPanel(alerts: Alert[], openCount = alerts.length) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <AlertsPanel alerts={alerts} openCount={openCount} note={DISCLAIMER} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.clearAllMocks());

describe('AlertsPanel', () => {
  it('shows the alert message and its transaction', () => {
    renderPanel([makeAlert()]);
    expect(screen.getByText(/much larger than your usual Dining spending/)).toBeInTheDocument();
    expect(screen.getByText(/Jul 23/)).toBeInTheDocument();
  });

  it('labels the alert type in plain language', () => {
    renderPanel([makeAlert()]);
    expect(screen.getByText('Unusually large')).toBeInTheDocument();
  });

  it('exposes the evidence so an alert can be audited', async () => {
    const user = userEvent.setup();
    renderPanel([makeAlert()]);

    await user.click(screen.getByText('Why this was flagged'));

    expect(screen.getByText(/median absolute deviation/)).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
    expect(screen.getByText('19.85')).toBeInTheDocument();
  });

  it('never presents an anomaly as confirmed fraud', () => {
    const alert = makeAlert();
    renderPanel([alert]);

    // The alert's own wording makes no accusation...
    expect(alert.message.toLowerCase()).not.toContain('fraud');
    expect(screen.getByText(alert.message)).toBeInTheDocument();
    // ...and the standing disclaimer is always on screen.
    expect(screen.getByText(DISCLAIMER)).toBeInTheDocument();
  });

  it('dismisses an alert through the API', async () => {
    const user = userEvent.setup();
    mockApi.updateAlert.mockResolvedValue({});
    renderPanel([makeAlert()]);

    await user.click(screen.getByRole('button', { name: /Dismiss/ }));

    await waitFor(() => expect(mockApi.updateAlert).toHaveBeenCalledWith('alert-1', 'dismissed'));
  });

  it('links through to review the transaction', () => {
    renderPanel([makeAlert()]);
    const link = screen.getByRole('link', { name: /Review transaction/ });
    expect(link).toHaveAttribute('href', expect.stringContaining('Blue%20Bottle%20Coffee'));
  });

  it('shows a calm empty state when nothing is flagged', () => {
    renderPanel([], 0);
    expect(screen.getByText('No open alerts')).toBeInTheDocument();
    expect(screen.getByText(/Nothing stands out/)).toBeInTheDocument();
  });

  it('renders every alert type with a readable label', () => {
    renderPanel([
      makeAlert({ id: 'a', alert_type: 'duplicate', severity: 'high' }),
      makeAlert({ id: 'b', alert_type: 'near_duplicate', severity: 'high' }),
      makeAlert({ id: 'c', alert_type: 'new_merchant', severity: 'low' }),
      makeAlert({ id: 'd', alert_type: 'large_for_merchant' }),
    ]);
    for (const label of [
      'Possible duplicate',
      'Charged twice?',
      'First time here',
      'Large for this merchant',
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });
});

describe('AlertsPanel — priority presentation', () => {
  const mixed = [
    makeAlert({ id: 'low-1', alert_type: 'new_merchant', severity: 'low' }),
    makeAlert({ id: 'med-1', alert_type: 'unusual_amount', severity: 'medium' }),
    makeAlert({ id: 'high-1', alert_type: 'near_duplicate', severity: 'high' }),
    makeAlert({ id: 'high-2', alert_type: 'duplicate', severity: 'high' }),
  ];

  it('groups alerts into three priority bands', () => {
    renderPanel(mixed);
    expect(screen.getByRole('region', { name: 'Worth reviewing' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Unusual for you' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'For information' })).toBeInTheDocument();
  });

  it('puts duplicates first, ahead of informational notes', () => {
    renderPanel(mixed);
    const headings = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
    expect(headings).toEqual(['Worth reviewing', 'Unusual for you', 'For information']);
  });

  it('counts the alerts in each band', () => {
    renderPanel(mixed);
    const high = screen.getByRole('region', { name: 'Worth reviewing' });
    expect(within(high).getByText('2')).toBeInTheDocument();
  });

  it('describes each band without asserting wrongdoing', () => {
    renderPanel(mixed);
    expect(screen.getByText('These look like the same charge appearing twice.')).toBeInTheDocument();
    expect(
      screen.getByText(/Larger than your own history would suggest. Often perfectly normal./),
    ).toBeInTheDocument();
    expect(screen.getByText('Noted in passing — nothing to act on.')).toBeInTheDocument();
  });

  it('shows the per-alert severity note from the backend', () => {
    renderPanel([makeAlert({ severity_note: 'For information only.' })]);
    expect(screen.getByText('For information only.')).toBeInTheDocument();
  });

  it('omits a band entirely when it has no alerts', () => {
    renderPanel([makeAlert({ id: 'only-low', severity: 'low' })]);
    expect(screen.getByRole('region', { name: 'For information' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Worth reviewing' })).not.toBeInTheDocument();
  });

  it('still shows the not-fraud disclaimer alongside the priorities', () => {
    renderPanel(mixed);
    expect(screen.getByText(DISCLAIMER)).toBeInTheDocument();
  });
});

describe('AlertsPanel — honest truncation', () => {
  it('says how many of the open alerts it is actually showing', () => {
    // The panel deliberately carries only the most serious alerts; implying it
    // has them all would hide the rest with no way to reach them.
    renderPanel([makeAlert({ id: 'a' }), makeAlert({ id: 'b' })], 30);
    expect(screen.getByText('Showing 2 of 30 alerts, most serious first.')).toBeInTheDocument();
  });

  it('links somewhere the rest can be reviewed', () => {
    renderPanel([makeAlert({ id: 'a' })], 30);
    const link = screen.getByRole('link', { name: /View all flagged transactions/ });
    // NOT ?review=needs_review — that is the low-confidence categorization
    // queue, which has no relationship to which transactions carry alerts.
    expect(link).toHaveAttribute('href', '/transactions?flagged=1');
  });

  it('says nothing when it is showing everything', () => {
    renderPanel([makeAlert({ id: 'a' }), makeAlert({ id: 'b' })], 2);
    expect(screen.queryByText(/Showing \d+ of/)).not.toBeInTheDocument();
  });

  it('says nothing when there are no alerts at all', () => {
    renderPanel([], 0);
    expect(screen.queryByText(/Showing \d+ of/)).not.toBeInTheDocument();
  });
});
