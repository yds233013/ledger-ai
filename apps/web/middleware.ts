/**
 * Route protection, over two independent session systems.
 *
 * The decision itself lives in `lib/route-access`, which imports nothing and is
 * unit tested. This file is only the wiring between that decision and the two
 * runtimes — Auth.js for the demo, Clerk for invited beta accounts.
 *
 * **Clerk is optional and must stay optional.** `clerkMiddleware` throws when
 * no publishable key is present, and this middleware runs on every request, so
 * an unconfigured deployment would 500 on its own landing page. The export is
 * therefore chosen once at module load: with no key, the Auth.js-only path is
 * used and is byte-for-byte the behaviour that shipped before Clerk existed.
 * The demo cannot be broken by Clerk being absent, half-configured, or down.
 */
import { clerkMiddleware } from '@clerk/nextjs/server';
import { NextResponse } from 'next/server';
import type { NextRequest } from 'next/server';
import { getToken } from 'next-auth/jwt';

import { auth } from '@/auth';
import { decide, type SessionSignals } from '@/lib/route-access';

/**
 * Decided at module load, not per request: the key cannot appear later, and a
 * per-request check would leave `clerkMiddleware` constructed either way.
 */
const clerkConfigured = Boolean(process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY);

/**
 * API routes are matched only so Clerk can attach its session context to
 * /api/auth/token. They must never be redirected: Auth.js owns /api/auth/*,
 * and turning one of its endpoints into a 307 would break the demo sign-in.
 */
function isApiPath(pathname: string): boolean {
  return pathname === '/api' || pathname.startsWith('/api/');
}

function apply(request: NextRequest, signals: SessionSignals) {
  if (isApiPath(request.nextUrl.pathname)) return NextResponse.next();

  const decision = decide(request.nextUrl.pathname, signals);

  if (decision.action === 'redirect') {
    const url = new URL(decision.to, request.nextUrl.origin);
    if (decision.callbackUrl) url.searchParams.set('callbackUrl', decision.callbackUrl);
    return NextResponse.redirect(url);
  }

  return NextResponse.next();
}

/** Auth.js only. The pre-Clerk behaviour, preserved exactly. */
const demoOnlyMiddleware = auth((request) => apply(request, { authjs: request.auth }));

/**
 * Both systems. Clerk wraps the request so its session is resolvable, and the
 * Auth.js session is read with `getToken`, which verifies the HS256 cookie
 * against AUTH_SECRET rather than trusting its presence.
 *
 * Reading both and handing them to one decision is what keeps them separate:
 * neither runtime ever sees the other's credential, and the tagged principal is
 * resolved in one place from two independently verified signals.
 */
function buildClerkMiddleware() {
  // Importing is harmless; constructing is not. `clerkMiddleware` reads the
  // publishable key when it runs, so this is only ever called behind the guard.
  return clerkMiddleware(async (clerkAuth, request) => {
    const { userId } = await clerkAuth();
    if (isApiPath(request.nextUrl.pathname)) return NextResponse.next();
    const authjs = await getToken({
      req: request,
      secret: process.env.AUTH_SECRET,
      // The demo cookie is host-prefixed in production and plain locally.
      secureCookie: request.nextUrl.protocol === 'https:',
    });

    // getToken returns the decoded JWT payload, whose `sub` is the Ledger AI
    // user id. Shaped into the same {user:{id}} the Auth.js session uses so the
    // pure decision has one input contract.
    const demoSession = authjs?.sub ? { user: { id: authjs.sub } } : null;

    return apply(request, { authjs: demoSession, clerkUserId: userId });
  });
}

export default clerkConfigured ? buildClerkMiddleware() : demoOnlyMiddleware;

export const config = {
  matcher: [
    '/((?!api|_next/static|_next/image|favicon.ico).*)',
    // Clerk resolves a session only for requests its middleware has seen, and
    // this route has to read one. It is passed straight through above.
    '/api/auth/token',
  ],
};
