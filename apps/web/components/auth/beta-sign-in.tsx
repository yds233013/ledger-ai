'use client';

import { SignIn } from '@clerk/nextjs';

import { clerkConfigured } from '@/lib/clerk-config';

/**
 * Clerk's sign-in card, mounted only where Clerk is configured.
 *
 * Importing `SignIn` is harmless; rendering it without a publishable key is
 * not. The page already checks `clerkConfigured` before reaching here; this
 * second check is
 * the belt to that brace, because a client component can be rendered from
 * somewhere the author did not anticipate.
 *
 * Only sign-in is offered. Clerk is in invite-only mode, so its sign-up flow
 * refuses uninvited addresses, and the API refuses any verified address with no
 * matching local invitation. A sign-up form here would be a route to two
 * refusals rather than an account.
 */
export function BetaSignIn() {
  if (!clerkConfigured) {
    return (
      <p data-testid="beta-unavailable" className="text-sm text-ink-muted">
        Beta sign-in is not available on this deployment.
      </p>
    );
  }

  return (
    <div data-testid="beta-sign-in">
      <SignIn
        routing="hash"
        signUpUrl="/beta"
        // Land on the dashboard; the API decides whether a profile exists.
        fallbackRedirectUrl="/dashboard"
        appearance={{ elements: { footerAction: { display: 'none' } } }}
      />
    </div>
  );
}
