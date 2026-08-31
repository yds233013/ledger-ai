/**
 * Clerk and Auth.js side by side, and the rules that keep them apart.
 *
 * Two session systems now reach the same routing decision. A holder of one must
 * never be treated as a holder of the other, in either direction, and neither
 * may be conjured from the absence of the other. These tests pin that, plus the
 * fail-closed behaviour that GHSA-8fpg-xm3f-6cx3 made concrete.
 */
import { describe, expect, it } from 'vitest';

import {
  BETA_BLOCKED_ROUTE_PREFIXES,
  decide,
  identify,
  PUBLIC_ROUTE_PREFIXES,
} from '@/lib/route-access';

const DEMO = { user: { id: 'demo-9f2c1a44-0000-4000-8000-000000000000' } };
const CLERK_ID = 'user_2abcdefghijklmnopqrst';

/** The truthy error payload from GHSA-8fpg-xm3f-6cx3. */
const CONFIG_ERROR = { message: 'There was a problem with the server configuration' };

const PROTECTED = ['/dashboard', '/transactions', '/receipts', '/settings', '/ask', '/'];

describe('identify — one principal, never a blend', () => {
  it('reads an Auth.js session as a demo principal', () => {
    expect(identify({ authjs: DEMO })).toEqual({ kind: 'demo', userId: DEMO.user.id });
  });

  it('reads a Clerk user id as a beta principal', () => {
    expect(identify({ clerkUserId: CLERK_ID })).toEqual({
      kind: 'beta',
      clerkUserId: CLERK_ID,
    });
  });

  it('reads neither as anonymous', () => {
    expect(identify({})).toEqual({ kind: 'anonymous' });
    expect(identify({ authjs: null, clerkUserId: null })).toEqual({ kind: 'anonymous' });
  });

  it('refuses to choose when both are present', () => {
    // Picking either would silently decide whether the request belongs to a
    // synthetic account or a real person's records.
    expect(identify({ authjs: DEMO, clerkUserId: CLERK_ID })).toEqual({ kind: 'conflict' });
  });

  it('never invents a demo principal from a Clerk session', () => {
    const principal = identify({ clerkUserId: CLERK_ID });
    expect(principal.kind).toBe('beta');
    expect(principal).not.toHaveProperty('userId');
  });

  it('never invents a beta principal from a demo session', () => {
    const principal = identify({ authjs: DEMO });
    expect(principal.kind).toBe('demo');
    expect(principal).not.toHaveProperty('clerkUserId');
  });
});

describe('identify — nothing malformed counts as a session', () => {
  it.each([
    ['the configuration-error object', { authjs: CONFIG_ERROR }],
    ['an empty session', { authjs: {} }],
    ['a session with no user id', { authjs: { user: { email: 'x@example.com' } } }],
    ['an empty user id', { authjs: { user: { id: '' } } }],
    ['a non-string user id', { authjs: { user: { id: 42 } } }],
    ['a Clerk id that is an object', { clerkUserId: { id: CLERK_ID } }],
    ['an empty Clerk id', { clerkUserId: '' }],
    ['a numeric Clerk id', { clerkUserId: 12345 }],
  ])('rejects %s', (_label, signals) => {
    expect(identify(signals)).toEqual({ kind: 'anonymous' });
  });
});

describe('protected routes fail closed', () => {
  it.each(PROTECTED)('sends an anonymous request from %s to sign-in', (route) => {
    expect(decide(route, {})).toEqual({
      action: 'redirect',
      to: '/sign-in',
      callbackUrl: route,
    });
  });

  it.each(PROTECTED)('does not let a configuration error open %s', (route) => {
    expect(decide(route, { authjs: CONFIG_ERROR })).toEqual({
      action: 'redirect',
      to: '/sign-in',
      callbackUrl: route,
    });
  });

  it.each(PROTECTED)('sends a conflicting pair from %s back to sign-in', (route) => {
    expect(decide(route, { authjs: DEMO, clerkUserId: CLERK_ID })).toEqual({
      action: 'redirect',
      to: '/sign-in',
    });
  });
});

describe('both kinds of session reach the app', () => {
  it.each(PROTECTED)('a demo session may load %s', (route) => {
    expect(decide(route, { authjs: DEMO })).toEqual({ action: 'next' });
  });

  it.each(['/dashboard', '/transactions', '/receipts', '/settings', '/ask'])(
    'a beta session may load %s',
    (route) => {
      expect(decide(route, { clerkUserId: CLERK_ID })).toEqual({ action: 'next' });
    },
  );
});

describe('uploads', () => {
  it('are open to persistent accounts now that quotas and screening exist', () => {
    // This route was closed to beta sessions for one phase, because it is where
    // real financial records enter and nothing yet bounded what they could
    // consume or contained what they could carry. Both now exist.
    expect(decide('/upload', { clerkUserId: CLERK_ID })).toEqual({ action: 'next' });
  });

  it('are open to demo sessions, whose data is synthetic and expires', () => {
    expect(decide('/upload', { authjs: DEMO })).toEqual({ action: 'next' });
  });

  it.each(BETA_BLOCKED_ROUTE_PREFIXES)('a beta session cannot reach %s', (route) => {
    // Empty today. Kept so that closing a route again is one line plus a test
    // that already covers it.
    expect(decide(route, { clerkUserId: CLERK_ID })).toEqual({
      action: 'redirect',
      to: '/dashboard',
    });
  });
});

describe('the public entry pages', () => {
  it.each(PUBLIC_ROUTE_PREFIXES)('%s is reachable while anonymous', (route) => {
    expect(decide(route, {})).toEqual({ action: 'next' });
  });

  it.each(PUBLIC_ROUTE_PREFIXES)('%s stays reachable during a conflict', (route) => {
    // Otherwise a contradictory cookie pair would lock the visitor out of the
    // only pages that could clear it.
    expect(decide(route, { authjs: DEMO, clerkUserId: CLERK_ID })).toEqual({
      action: 'next',
    });
  });

  it.each(PUBLIC_ROUTE_PREFIXES)('a demo session is moved off %s', (route) => {
    expect(decide(route, { authjs: DEMO })).toEqual({
      action: 'redirect',
      to: '/dashboard',
    });
  });

  it.each(PUBLIC_ROUTE_PREFIXES)('a beta session is moved off %s', (route) => {
    expect(decide(route, { clerkUserId: CLERK_ID })).toEqual({
      action: 'redirect',
      to: '/dashboard',
    });
  });

  it('keeps sub-paths of a public prefix public', () => {
    expect(decide('/beta/factor-one', {})).toEqual({ action: 'next' });
    expect(decide('/sign-in/anything', {})).toEqual({ action: 'next' });
  });
});

describe('callback URLs', () => {
  it('remembers where an anonymous visitor was going', () => {
    expect(decide('/transactions', {})).toMatchObject({ callbackUrl: '/transactions' });
  });

  it('carries no callback when resolving a conflict', () => {
    // There is nowhere sensible to return to until the visitor picks one.
    expect(decide('/transactions', { authjs: DEMO, clerkUserId: CLERK_ID })).not.toHaveProperty(
      'callbackUrl',
    );
  });

  it('carries no callback when redirecting a signed-in visitor off sign-in', () => {
    expect(decide('/sign-in', { authjs: DEMO })).not.toHaveProperty('callbackUrl');
  });
});
