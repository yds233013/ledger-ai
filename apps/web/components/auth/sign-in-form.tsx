'use client';

import { signIn } from 'next-auth/react';
import { useRouter, useSearchParams } from 'next/navigation';
import { useState } from 'react';

import { Spinner } from '@/components/ui/primitives';

/**
 * Three ways in, ordered by what a first-time visitor most likely wants.
 *
 * The demo is the primary action and deliberately the largest target: a
 * portfolio reviewer should not have to read a credentials box to get into a
 * populated app. Credentials sit below it for local development, and GitHub
 * appears only when the deployment has actually been configured for it.
 */
export function SignInForm({ githubEnabled }: { githubEnabled: boolean }) {
  const router = useRouter();
  const params = useSearchParams();
  const callbackUrl = params.get('callbackUrl') ?? '/dashboard';
  const demoExpired = params.get('demo') === 'expired';

  const [email, setEmail] = useState('demo@ledgerai.local');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<'credentials' | 'demo' | 'github' | null>(null);

  async function handleCredentials(event: React.FormEvent) {
    event.preventDefault();
    setPending('credentials');
    setError(null);

    const result = await signIn('credentials', { email, password, redirect: false });

    if (result?.error) {
      setError('Incorrect email or password.');
      setPending(null);
      return;
    }
    router.push(callbackUrl);
    router.refresh();
  }

  async function handleDemo() {
    setPending('demo');
    setError(null);

    const result = await signIn('demo', { redirect: false });

    if (result?.error) {
      // The provisioning endpoint is rate limited, and that is the failure a
      // visitor is most likely to hit, so it is named rather than hidden
      // behind a generic message.
      setError(
        'Could not start a demo just now. Several demos have been started from this ' +
          'network recently — please wait a few minutes and try again.',
      );
      setPending(null);
      return;
    }
    router.push('/dashboard');
    router.refresh();
  }

  return (
    <div className="space-y-5">
      {demoExpired ? (
        <div
          role="status"
          data-testid="demo-expired-notice"
          className="rounded-lg border border-line bg-surface-sunken p-3"
        >
          <p className="text-xs font-medium text-ink">Your demo session has ended</p>
          <p className="mt-1 text-xs leading-relaxed text-ink-muted">
            Demo accounts last 24 hours, after which the account and all of its
            synthetic data are deleted. Start a new one below — it takes a moment to
            build.
          </p>
        </div>
      ) : null}

      <div>
        <button
          type="button"
          onClick={handleDemo}
          disabled={pending !== null}
          className="btn-primary w-full"
          data-testid="try-demo"
        >
          {pending === 'demo' ? <Spinner /> : null}
          {pending === 'demo' ? 'Building your demo…' : 'Try the demo'}
        </button>
        <p className="mt-2 text-xs leading-relaxed text-ink-muted">
          Creates a private account with about 250 synthetic transactions across eight
          months. Nothing is shared with other visitors, and it is deleted automatically
          after 24 hours.
        </p>
      </div>

      {error ? (
        <p role="alert" className="text-sm text-negative">
          {error}
        </p>
      ) : null}

      <div className="flex items-center gap-3" aria-hidden="true">
        <span className="h-px flex-1 bg-line" />
        <span className="text-xs text-ink-faint">or sign in</span>
        <span className="h-px flex-1 bg-line" />
      </div>

      {githubEnabled ? (
        <button
          type="button"
          onClick={() => {
            setPending('github');
            void signIn('github', { callbackUrl });
          }}
          disabled={pending !== null}
          className="btn-secondary w-full"
        >
          {pending === 'github' ? <Spinner /> : null}
          Continue with GitHub
        </button>
      ) : null}

      <form onSubmit={handleCredentials} className="space-y-4">
        <div>
          <label htmlFor="email" className="label">
            Email
          </label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="input"
          />
        </div>

        <div>
          <label htmlFor="password" className="label">
            Password
          </label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="input"
          />
        </div>

        <button
          type="submit"
          disabled={pending !== null}
          className="btn-secondary w-full"
        >
          {pending === 'credentials' ? <Spinner /> : null}
          {pending === 'credentials' ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  );
}
