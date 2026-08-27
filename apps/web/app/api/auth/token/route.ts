/**
 * Mint a short-lived API access token from the current Auth.js session.
 *
 * The browser never holds a long-lived credential: the session cookie is
 * httpOnly, and this route exchanges it server-side for a 15-minute HS256
 * bearer token that FastAPI verifies with the same AUTH_SECRET.
 */
import { SignJWT } from 'jose';
import { NextResponse } from 'next/server';

import { auth } from '@/auth';

const TTL_SECONDS = 15 * 60;

export async function GET() {
  const session = await auth();
  if (!session?.user?.id) {
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401 });
  }

  const secret = process.env.AUTH_SECRET;
  if (!secret) {
    return NextResponse.json({ error: 'AUTH_SECRET is not configured' }, { status: 500 });
  }

  const now = Math.floor(Date.now() / 1000);
  const token = await new SignJWT({ email: session.user.email ?? '' })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(session.user.id)
    .setIssuer('ledgerai')
    .setAudience('ledgerai-api')
    .setIssuedAt(now)
    .setExpirationTime(now + TTL_SECONDS)
    .sign(new TextEncoder().encode(secret));

  return NextResponse.json(
    { access_token: token, expires_in: TTL_SECONDS },
    { headers: { 'Cache-Control': 'no-store' } },
  );
}
