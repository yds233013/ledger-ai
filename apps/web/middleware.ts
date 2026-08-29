/**
 * Route protection. Everything under the app shell requires a session; the
 * sign-in page and auth endpoints stay public.
 *
 * The decision itself lives in `lib/route-access`, which has no imports and is
 * unit tested. This file is only the wiring between that decision and the
 * Auth.js runtime.
 */
import { NextResponse } from 'next/server';

import { auth } from '@/auth';
import { decide } from '@/lib/route-access';

export default auth((request) => {
  const decision = decide(request.nextUrl.pathname, request.auth);

  if (decision.action === 'redirect') {
    const url = new URL(decision.to, request.nextUrl.origin);
    if (decision.callbackUrl) url.searchParams.set('callbackUrl', decision.callbackUrl);
    return NextResponse.redirect(url);
  }

  return NextResponse.next();
});

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.ico).*)'],
};
