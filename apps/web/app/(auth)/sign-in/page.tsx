import { Suspense } from 'react';

import { githubEnabled } from '@/auth';
import { SignInForm } from '@/components/auth/sign-in-form';

/**
 * A server component, so it can read whether GitHub OAuth is configured.
 *
 * `githubEnabled` depends on server-only secrets, which must never reach the
 * browser bundle. Resolving it here and passing a boolean down means an
 * unconfigured deployment renders no GitHub button at all, rather than one
 * that fails after the user commits to it.
 */
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
          <Suspense fallback={<div className="h-80" />}>
            <SignInForm githubEnabled={githubEnabled} />
          </Suspense>
        </div>

        <p className="mt-4 px-1 text-xs leading-relaxed text-ink-faint">
          Every figure in Ledger AI&rsquo;s demo is synthetic and generated for
          demonstration. It does not describe any real person, account or payment, and
          Ledger AI does not connect to any financial institution.
        </p>
      </div>
    </div>
  );
}
