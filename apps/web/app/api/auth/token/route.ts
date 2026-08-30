/**
 * Mint or fetch the bearer token the browser sends to FastAPI.
 *
 * Two kinds of caller, two entirely different tokens, and this route is where
 * the two are kept from crossing:
 *
 *   demo — an Auth.js session. A short-lived HS256 token is minted here and
 *          verified by FastAPI with the shared AUTH_SECRET.
 *   beta — a Clerk session. Clerk's own RS256 session token is returned as-is.
 *          It is never minted here; this process holds no key that could sign
 *          one, which is what makes forging one impossible from this side.
 *
 * The API verifies these on separate paths with no fallback between them, so a
 * token of one family can never authenticate as the other. This route upholds
 * the same rule at the source: a demo session can only ever receive an HS256
 * token, and a Clerk session can only ever receive Clerk's.
 *
 * Holding both is refused outright. Choosing one would be silently deciding
 * whether a request belongs to a throwaway synthetic account or a real person's
 * financial records — exactly the substitution this design exists to prevent.
 */
import { auth as clerkAuth } from '@clerk/nextjs/server';
import { SignJWT } from 'jose';
import { NextResponse } from 'next/server';

import { auth } from '@/auth';
import { clerkConfigured } from '@/lib/clerk-config';
import { identify } from '@/lib/route-access';

const TTL_SECONDS = 15 * 60;

const NO_STORE = { 'Cache-Control': 'no-store' } as const;

async function clerkSession(): Promise<{ userId: string | null; getToken: () => Promise<string | null> }> {
  if (!clerkConfigured) return { userId: null, getToken: async () => null };
  try {
    const resolved = await clerkAuth();
    return { userId: resolved.userId, getToken: () => resolved.getToken() };
  } catch {
    // Clerk throws when its middleware has not seen the request. A demo user
    // has no Clerk session anyway, and their token must not depend on Clerk
    // being reachable, correctly matched, or up. Degrade to "no Clerk session"
    // rather than turning the demo's only credential endpoint into a 500.
    return { userId: null, getToken: async () => null };
  }
}

export async function GET() {
  const [session, clerk] = await Promise.all([auth(), clerkSession()]);
  const principal = identify({ authjs: session, clerkUserId: clerk.userId });

  if (principal.kind === 'beta') {
    // Clerk's session token, untouched. FastAPI resolves it to a Ledger AI
    // profile through the invitation-bound provisioning flow, or refuses it.
    const token = await clerk.getToken();
    if (!token) {
      return NextResponse.json(
        { error: 'Not authenticated' },
        { status: 401, headers: NO_STORE },
      );
    }
    return NextResponse.json(
      { access_token: token, expires_in: 60 },
      { headers: NO_STORE },
    );
  }

  if (principal.kind !== 'demo') {
    // anonymous, or holding both credentials at once.
    return NextResponse.json({ error: 'Not authenticated' }, { status: 401, headers: NO_STORE });
  }

  const secret = process.env.AUTH_SECRET;
  if (!secret) {
    return NextResponse.json(
      { error: 'AUTH_SECRET is not configured' },
      { status: 500, headers: NO_STORE },
    );
  }

  const now = Math.floor(Date.now() / 1000);
  const token = await new SignJWT({ email: session?.user?.email ?? '' })
    .setProtectedHeader({ alg: 'HS256' })
    .setSubject(principal.userId)
    .setIssuer('ledgerai')
    .setAudience('ledgerai-api')
    .setIssuedAt(now)
    .setExpirationTime(now + TTL_SECONDS)
    .sign(new TextEncoder().encode(secret));

  return NextResponse.json(
    { access_token: token, expires_in: TTL_SECONDS },
    { headers: NO_STORE },
  );
}
