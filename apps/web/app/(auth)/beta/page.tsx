import Link from 'next/link';

import { BetaSignIn } from '@/components/auth/beta-sign-in';
import { clerkConfigured } from '@/lib/clerk-config';

/**
 * Sign-in for invited beta accounts.
 *
 * Separate from `/sign-in` on purpose. The demo and the beta are different
 * kinds of account with different data and different risks, and giving them one
 * combined form would invite exactly the confusion the rest of this integration
 * works to prevent. A visitor picks a path, and each path uses one credential.
 *
 * There is no sign-up. Clerk's instance is in invite-only mode and the API
 * additionally requires a local invitation bound to the verified address, so an
 * uninvited person who somehow reached a sign-up form would authenticate and
 * then be refused an account. Saying so up front is kinder than that.
 */
export default function BetaSignInPage() {
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
            <h1 className="text-lg font-semibold tracking-tight">Ledger AI private beta</h1>
            <p className="text-xs text-ink-muted">Invitation required</p>
          </div>
        </div>

        <div className="card p-6">
          {clerkConfigured ? (
            <BetaSignIn />
          ) : (
            <div data-testid="beta-unavailable" className="space-y-3">
              <p className="text-sm font-medium text-ink">Beta sign-in is not available</p>
              <p className="text-xs leading-relaxed text-ink-muted">
                This deployment has no beta authentication configured. The 24-hour demo
                is unaffected.
              </p>
            </div>
          )}
        </div>

        <p className="mt-4 px-1 text-xs leading-relaxed text-ink-faint">
          Not invited?{' '}
          <Link href="/sign-in" className="underline">
            Try the 24-hour demo
          </Link>{' '}
          instead — it needs no account and uses entirely synthetic data.
        </p>
      </div>
    </div>
  );
}
