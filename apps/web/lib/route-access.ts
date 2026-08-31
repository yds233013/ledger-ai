/**
 * Who may see which route.
 *
 * Two entirely separate ways to be signed in reach this file, and the whole
 * point of it is that they never blur into each other:
 *
 *   demo — an Auth.js session, HS256, cookie `authjs.session-token`. Ephemeral,
 *          synthetic data, deletes itself within a day.
 *   beta — a Clerk session, RS256, Clerk's own cookie. A real invited person
 *          with real financial records.
 *
 * They are different credentials, verified by different code, for different
 * kinds of account. A holder of one must never be treated as a holder of the
 * other, in either direction — which is why `identify` returns a tagged
 * principal rather than a boolean, and why holding both at once is an outcome
 * of its own rather than a coin flip.
 *
 * Kept apart from `middleware.ts` because that module imports the Auth.js and
 * Clerk runtimes, which only resolve inside a Next.js build. This file imports
 * nothing, so every rule below is unit tested directly. GHSA-8fpg-xm3f-6cx3 is
 * the standing argument for that: the fail-open it described lived in exactly
 * this decision.
 */

export type RouteDecision =
  | { action: 'next' }
  | { action: 'redirect'; to: string; callbackUrl?: string };

/** Which kind of account is making the request, if any. */
export type Principal =
  | { kind: 'anonymous' }
  | { kind: 'demo'; userId: string }
  | { kind: 'beta'; clerkUserId: string }
  | { kind: 'conflict' };

/** Whatever the two runtimes hand the middleware, untyped on purpose. */
export interface SessionSignals {
  /** The Auth.js session object, or null. */
  authjs?: unknown;
  /** Clerk's resolved user id, or null. Never an object. */
  clerkUserId?: unknown;
}

/** Routes that stay reachable without any session at all. */
const PUBLIC_PREFIXES = ['/sign-in', '/beta'] as const;

/**
 * Routes a beta (Clerk) account may not reach yet.
 *
 * Empty, and deliberately kept rather than deleted. `/upload` sat here for one
 * phase because uploads are how real financial records enter the system and the
 * quotas and sensitive-identifier rejection that must guard that path did not
 * exist yet. They do now — per-account budgets, whole-file refusal of unmasked
 * identifiers, and a consent gate in front of the form — so the route is open.
 *
 * The list stays because "close a route to persistent accounts until the thing
 * that protects it ships" is a move worth being able to make again in one line.
 */
const BETA_BLOCKED_PREFIXES: readonly string[] = [];

function hasPrefix(pathname: string, prefixes: readonly string[]): boolean {
  return prefixes.some((p) => pathname === p || pathname.startsWith(`${p}/`));
}

/**
 * Read an Auth.js session into a demo user id.
 *
 * Deliberately `user.id` rather than the truthiness of the session object.
 * Under GHSA-8fpg-xm3f-6cx3 a configuration error made that object a truthy
 * error payload, so every `!auth` check read as "signed in". An error payload
 * has no `user.id`, so this fails closed on any version of the library.
 */
function demoUserId(authjs: unknown): string | null {
  const user = (authjs as { user?: { id?: unknown } } | null | undefined)?.user;
  return typeof user?.id === 'string' && user.id.length > 0 ? user.id : null;
}

/** Clerk hands back a string id or nothing. Anything else is not a session. */
function betaUserId(clerkUserId: unknown): string | null {
  return typeof clerkUserId === 'string' && clerkUserId.length > 0 ? clerkUserId : null;
}

/**
 * Resolve the two signals to exactly one principal.
 *
 * Holding both credentials at once is not a state this application can produce
 * — the sign-in page offers one or the other and each flow clears the other's
 * cookie — so if it happens something is wrong. Picking one would be choosing
 * silently between a synthetic account and a real one, which is precisely the
 * substitution this module exists to prevent. It resolves to `conflict`, and
 * the caller sends the request back to sign-in to start cleanly.
 */
export function identify(signals: SessionSignals | null | undefined): Principal {
  const demo = demoUserId(signals?.authjs);
  const beta = betaUserId(signals?.clerkUserId);

  if (demo && beta) return { kind: 'conflict' };
  if (beta) return { kind: 'beta', clerkUserId: beta };
  if (demo) return { kind: 'demo', userId: demo };
  return { kind: 'anonymous' };
}

/** Where a request should go, given its path and whatever sessions it carries. */
export function decide(
  pathname: string,
  signals: SessionSignals | null | undefined,
): RouteDecision {
  const principal = identify(signals);
  const isPublic = hasPrefix(pathname, PUBLIC_PREFIXES);

  // A contradictory pair is resolved by no one but the visitor, at sign-in.
  if (principal.kind === 'conflict') {
    return isPublic ? { action: 'next' } : { action: 'redirect', to: '/sign-in' };
  }

  if (principal.kind === 'anonymous') {
    return isPublic
      ? { action: 'next' }
      : { action: 'redirect', to: '/sign-in', callbackUrl: pathname };
  }

  // Signed in, on a public entry page: send them where they were going.
  if (isPublic) return { action: 'redirect', to: '/dashboard' };

  if (principal.kind === 'beta' && hasPrefix(pathname, BETA_BLOCKED_PREFIXES)) {
    return { action: 'redirect', to: '/dashboard' };
  }

  return { action: 'next' };
}

/** Exported for tests and for the sign-in page's copy. */
export const PUBLIC_ROUTE_PREFIXES = PUBLIC_PREFIXES;
export const BETA_BLOCKED_ROUTE_PREFIXES = BETA_BLOCKED_PREFIXES;

/**
 * Whether an Auth.js session object represents a signed-in demo user.
 *
 * Retained as the narrow, single-signal form of the rule that
 * GHSA-8fpg-xm3f-6cx3 turned into a vulnerability: ask for an identified user,
 * never for the object to merely exist.
 */
export function hasSession(authjs: unknown): boolean {
  return identify({ authjs }).kind === 'demo';
}
