/**
 * The sign-in surface.
 *
 * The demo is the primary way in for someone who has never seen Ledger AI, so
 * it has to be present, obvious, honest about what it creates, and it must not
 * hand the browser any credential.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SignInForm } from '@/components/auth/sign-in-form';

const nav = vi.hoisted(() => ({
  search: '',
  push: vi.fn(),
  refresh: vi.fn(),
}));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: nav.push, refresh: nav.refresh, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));

const mockSignIn = vi.hoisted(() => vi.fn());
vi.mock('next-auth/react', () => ({ signIn: mockSignIn }));

beforeEach(() => {
  vi.clearAllMocks();
  nav.search = '';
  mockSignIn.mockResolvedValue({ ok: true, error: undefined });
});

describe('demo entry', () => {
  it('offers a prominent "Try the 24-hour demo" action', () => {
    render(<SignInForm githubEnabled={false} />);
    expect(screen.getByRole('button', { name: /Try the 24-hour demo/i })).toBeInTheDocument();
  });

  it('says what the demo creates before it is clicked', () => {
    render(<SignInForm githubEnabled={false} />);
    const description = screen.getByText(/250 synthetic transactions/i);
    expect(description).toHaveTextContent(/eight months/i);
    expect(description).toHaveTextContent(/deleted automatically after 24 hours/i);
  });

  it('says the demo is private to the visitor', () => {
    render(<SignInForm githubEnabled={false} />);
    expect(screen.getByText(/Nothing is shared with other visitors/i)).toBeInTheDocument();
  });

  it('signs in through the demo provider', async () => {
    render(<SignInForm githubEnabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: /Try the 24-hour demo/i }));

    expect(mockSignIn).toHaveBeenCalledWith('demo', { redirect: false });
  });

  it('never sends credentials for the demo', async () => {
    render(<SignInForm githubEnabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: /Try the 24-hour demo/i }));

    const [, options] = mockSignIn.mock.calls[0];
    expect(options).not.toHaveProperty('email');
    expect(options).not.toHaveProperty('password');
  });

  it('shows progress while the account is being built', async () => {
    let resolve: (value: unknown) => void = () => {};
    mockSignIn.mockReturnValue(new Promise((r) => { resolve = r; }));

    render(<SignInForm githubEnabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: /Try the 24-hour demo/i }));

    expect(await screen.findByText(/Building your demo/i)).toBeInTheDocument();
    resolve({ ok: true });
  });

  it('lands on the dashboard when provisioning succeeds', async () => {
    render(<SignInForm githubEnabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: /Try the 24-hour demo/i }));

    await waitFor(() => expect(nav.push).toHaveBeenCalledWith('/dashboard'));
  });

  it('explains a throttled failure instead of failing silently', async () => {
    mockSignIn.mockResolvedValue({ error: 'CredentialsSignin' });

    render(<SignInForm githubEnabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: /Try the 24-hour demo/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/wait a few minutes/i);
  });

  it('re-enables the button after a failure so the visitor can retry', async () => {
    mockSignIn.mockResolvedValue({ error: 'CredentialsSignin' });

    render(<SignInForm githubEnabled={false} />);
    await userEvent.click(screen.getByRole('button', { name: /Try the 24-hour demo/i }));

    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: /Try the 24-hour demo/i })).toBeEnabled();
  });
});

describe('expired demo messaging', () => {
  it('explains why the visitor was signed out', () => {
    nav.search = 'demo=expired';
    render(<SignInForm githubEnabled={false} />);

    const notice = screen.getByTestId('demo-expired-notice');
    expect(notice).toHaveTextContent(/demo session has ended/i);
    expect(notice).toHaveTextContent(/24 hours/i);
  });

  it('shows nothing when the visitor simply arrived at sign-in', () => {
    render(<SignInForm githubEnabled={false} />);
    expect(screen.queryByTestId('demo-expired-notice')).not.toBeInTheDocument();
  });
});

describe('optional GitHub sign-in', () => {
  it('is absent when the deployment has no OAuth application', () => {
    render(<SignInForm githubEnabled={false} />);
    expect(screen.queryByRole('button', { name: /GitHub/i })).not.toBeInTheDocument();
  });

  it('appears when configured', () => {
    render(<SignInForm githubEnabled />);
    expect(screen.getByRole('button', { name: /Continue with GitHub/i })).toBeInTheDocument();
  });

  it('does not gate the demo behind GitHub', () => {
    render(<SignInForm githubEnabled />);
    // Both are available; the demo never requires authorising anything.
    expect(screen.getByRole('button', { name: /Try the 24-hour demo/i })).toBeEnabled();
  });

  it('uses the github provider when clicked', async () => {
    render(<SignInForm githubEnabled />);
    await userEvent.click(screen.getByRole('button', { name: /Continue with GitHub/i }));

    expect(mockSignIn).toHaveBeenCalledWith('github', { callbackUrl: '/dashboard' });
  });
});

describe('credentials sign-in', () => {
  it('remains available for local development', () => {
    render(<SignInForm githubEnabled={false} />);
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.getByLabelText('Password')).toBeInTheDocument();
  });

  it('does not prefill a password', () => {
    render(<SignInForm githubEnabled={false} />);
    expect(screen.getByLabelText('Password')).toHaveValue('');
  });

  it('reports a rejected sign-in without saying which field was wrong', async () => {
    mockSignIn.mockResolvedValue({ error: 'CredentialsSignin' });

    render(<SignInForm githubEnabled={false} />);
    await userEvent.type(screen.getByLabelText('Password'), 'wrong-password');
    await userEvent.click(screen.getByRole('button', { name: /^Sign in$/i }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Incorrect email or password.');
  });
});
