'use client';

import { signIn } from 'next-auth/react';
import { useRouter, useSearchParams } from 'next/navigation';
import { Suspense, useState } from 'react';

import { Spinner } from '@/components/ui/primitives';

function SignInForm() {
  const router = useRouter();
  const params = useSearchParams();
  const callbackUrl = params.get('callbackUrl') ?? '/dashboard';

  const [email, setEmail] = useState('demo@ledgerai.local');
  const [password, setPassword] = useState('demo1234');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    const result = await signIn('credentials', { email, password, redirect: false });

    if (result?.error) {
      setError('Incorrect email or password.');
      setSubmitting(false);
      return;
    }
    router.push(callbackUrl);
    router.refresh();
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
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

      {error ? (
        <p role="alert" className="text-sm text-negative">
          {error}
        </p>
      ) : null}

      <button type="submit" disabled={submitting} className="btn-primary w-full">
        {submitting ? <Spinner /> : null}
        {submitting ? 'Signing in…' : 'Sign in'}
      </button>
    </form>
  );
}

export default function SignInPage() {
  return (
    <div className="grid min-h-screen place-items-center px-4 py-12">
      <div className="w-full max-w-sm">
        <div className="mb-6 flex items-center gap-2">
          <span
            aria-hidden="true"
            className="grid h-9 w-9 place-items-center rounded-xl bg-brand text-base font-bold text-white"
          >
            L
          </span>
          <div>
            <h1 className="text-lg font-semibold tracking-tight">Ledger AI</h1>
            <p className="text-xs text-ink-muted">Personal finance data workspace</p>
          </div>
        </div>

        <div className="card p-6">
          <Suspense fallback={<div className="h-56" />}>
            <SignInForm />
          </Suspense>

          <div className="mt-5 rounded-lg border border-line bg-surface-sunken p-3">
            <p className="text-xs font-medium text-ink">Demo account</p>
            <p className="mt-1 text-xs text-ink-muted">
              <code className="font-mono">demo@ledgerai.local</code> /{' '}
              <code className="font-mono">demo1234</code>
            </p>
            <p className="mt-2 text-xs text-ink-faint">
              All data in this account is synthetic and generated for demonstration.
            </p>
          </div>
        </div>
      </div>
    </div>
  );
}
