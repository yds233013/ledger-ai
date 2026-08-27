/**
 * Route protection. Everything under the app shell requires a session; the
 * sign-in page and auth endpoints stay public.
 */
import { NextResponse } from 'next/server';

import { auth } from '@/auth';

export default auth((request) => {
  const isSignIn = request.nextUrl.pathname.startsWith('/sign-in');

  if (!request.auth && !isSignIn) {
    const url = new URL('/sign-in', request.nextUrl.origin);
    url.searchParams.set('callbackUrl', request.nextUrl.pathname);
    return NextResponse.redirect(url);
  }

  if (request.auth && isSignIn) {
    return NextResponse.redirect(new URL('/dashboard', request.nextUrl.origin));
  }

  return NextResponse.next();
});

export const config = {
  matcher: ['/((?!api|_next/static|_next/image|favicon.ico).*)'],
};
