/**
 * The standing notice on an ephemeral demo account.
 *
 * A demo that stops working without warning reads as a broken app; saying how
 * long is left makes the ending an expected event.
 */
import { render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { DemoBanner } from '@/components/layout/demo-banner';

const mockSession = vi.hoisted(() => ({ value: null as unknown }));
vi.mock('next-auth/react', () => ({
  useSession: () => ({ data: mockSession.value }),
}));

function session(overrides: Record<string, unknown>) {
  return { user: { id: 'u1', email: 'demo@example.invalid', ...overrides } };
}

function inHours(hours: number): string {
  return new Date(Date.now() + hours * 3_600_000).toISOString();
}

beforeEach(() => {
  mockSession.value = null;
});

describe('DemoBanner', () => {
  it('renders nothing for a non-demo account', () => {
    mockSession.value = session({ isDemo: false });
    const { container } = render(<DemoBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when there is no session', () => {
    const { container } = render(<DemoBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing for a permanent demo account with no deadline', () => {
    // The seeded local development user: is_demo, but never expires.
    mockSession.value = session({ isDemo: true, demoExpiresAt: null });
    const { container } = render(<DemoBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says the data is synthetic', () => {
    mockSession.value = session({ isDemo: true, demoExpiresAt: inHours(12) });
    render(<DemoBanner />);
    expect(screen.getByTestId('demo-banner')).toHaveTextContent(/synthetic/i);
  });

  it('shows the time remaining in hours and minutes', () => {
    mockSession.value = session({ isDemo: true, demoExpiresAt: inHours(5.5) });
    render(<DemoBanner />);
    // 5h30m lands exactly on a minute boundary, and the remaining time is
    // floored — so this renders "5h 30m" or "5h 29m" depending on how many
    // milliseconds pass between building the deadline and rendering. What is
    // under test is that both units are shown, not which side of the boundary
    // the clock happened to fall on.
    expect(screen.getByTestId('demo-banner')).toHaveTextContent(/5h (29|30)m/);
  });

  it('drops to minutes in the final hour', () => {
    mockSession.value = session({ isDemo: true, demoExpiresAt: inHours(0.5) });
    render(<DemoBanner />);
    expect(screen.getByTestId('demo-banner')).toHaveTextContent(/^(?!.*\dh).*\d+m/);
  });

  it('says the account will be deleted', () => {
    mockSession.value = session({ isDemo: true, demoExpiresAt: inHours(3) });
    render(<DemoBanner />);
    expect(screen.getByTestId('demo-banner')).toHaveTextContent(/deleted automatically/i);
  });

  it('reports an already-expired demo as ended', () => {
    mockSession.value = session({ isDemo: true, demoExpiresAt: inHours(-1) });
    render(<DemoBanner />);

    const banner = screen.getByTestId('demo-banner');
    expect(banner).toHaveTextContent(/This demo has ended/i);
    expect(banner).toHaveTextContent(/start a new demo/i);
  });

  it('is announced to assistive technology', () => {
    mockSession.value = session({ isDemo: true, demoExpiresAt: inHours(3) });
    render(<DemoBanner />);
    expect(screen.getByRole('status')).toBeInTheDocument();
  });
});
