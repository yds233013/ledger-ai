/**
 * Who may see which route.
 *
 * Kept apart from `middleware.ts` on purpose. The middleware module imports
 * the Auth.js runtime, which drags in `next/server` and only resolves inside a
 * Next.js build — so a rule that lives there cannot be unit tested. This one
 * has no imports at all, and GHSA-8fpg-xm3f-6cx3 is the argument for why that
 * is worth a file: the fail-open it describes lived in precisely this decision,
 * and a decision nobody can call directly is a decision nobody can test.
 */

export type RouteDecision =
  | { action: 'next' }
  | { action: 'redirect'; to: string; callbackUrl?: string };

/**
 * Whether a request carries a real, identified session.
 *
 * Deliberately `auth?.user?.id` rather than `!!auth`. Under
 * GHSA-8fpg-xm3f-6cx3, an Auth.js configuration error — an unset AUTH_SECRET
 * being the usual cause — made the `auth` object a truthy error payload of the
 * shape `{ message: "There was a problem with the server configuration" }`.
 * Every `!auth` check then read as "signed in", and the middleware waved
 * through every anonymous request to every protected route.
 *
 * next-auth 5.0.0-beta.32 fixes that at the source, so a failed session lookup
 * now yields no session at all. This check does not depend on that fix: an
 * error payload has no `user`, so it fails closed on either version. The
 * library is the repair and this is the belt to its braces — not redundant,
 * because the next such bug will arrive without warning too.
 *
 * `user.id` specifically, not `user`, because the session is only useful to
 * this application once the jwt callback has resolved a Ledger AI user id.
 */
export function hasSession(auth: unknown): boolean {
  const user = (auth as { user?: { id?: unknown } } | null | undefined)?.user;
  return typeof user?.id === 'string' && user.id.length > 0;
}

/**
 * Where a request should go, given its path and whatever session it carries.
 */
export function decide(pathname: string, auth: unknown): RouteDecision {
  const isSignIn = pathname.startsWith('/sign-in');
  const signedIn = hasSession(auth);

  if (!signedIn && !isSignIn) {
    return { action: 'redirect', to: '/sign-in', callbackUrl: pathname };
  }

  if (signedIn && isSignIn) {
    return { action: 'redirect', to: '/dashboard' };
  }

  return { action: 'next' };
}
