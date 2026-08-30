/**
 * Middleware authorization, and the fail-open it must not have.
 *
 * GHSA-8fpg-xm3f-6cx3 (critical, next-auth >=5.0.0-beta.0 <=5.0.0-beta.31):
 * when the Auth.js configuration produced a server-side error, the `auth`
 * object handed to middleware was populated with an error payload instead of
 * being null. Objects are truthy, so the documented `!!auth` / `!req.auth`
 * check evaluated as "signed in" for *every* request, and every protected
 * route became public. An unset AUTH_SECRET was enough to trigger it.
 *
 * The dependency upgrade to 5.0.0-beta.32 fixes the cause. These tests pin the
 * consequence, which is the part that belongs to this repository: whatever the
 * library hands us, an object without an identified user must not be treated
 * as a session. They would fail against the old middleware on the old library,
 * and they keep failing if anyone reintroduces a bare truthiness check.
 */
import { describe, expect, it } from 'vitest';

import { decide, hasSession } from '@/lib/route-access';

/** The exact shape the advisory describes. */
const CONFIG_ERROR = { message: 'There was a problem with the server configuration' };

const REAL_SESSION = {
  user: { id: 'a3f1c2d4-0000-4000-8000-000000000000', email: 'demo@ledgerai.local' },
  expires: '2099-01-01T00:00:00.000Z',
};

describe('hasSession', () => {
  it('accepts a session carrying an identified user', () => {
    expect(hasSession(REAL_SESSION)).toBe(true);
  });

  it('rejects the configuration-error object from GHSA-8fpg-xm3f-6cx3', () => {
    // The regression. `!!CONFIG_ERROR` is true; this must still be false.
    expect(Boolean(CONFIG_ERROR)).toBe(true);
    expect(hasSession(CONFIG_ERROR)).toBe(false);
  });

  it.each([
    ['null', null],
    ['undefined', undefined],
    ['an empty object', {}],
    ['a session with no user', { expires: '2099-01-01T00:00:00.000Z' }],
    ['a user with no id', { user: { email: 'nobody@example.com' } }],
    ['a user with an empty id', { user: { id: '' } }],
    ['a user with a non-string id', { user: { id: 12345 } }],
    ['an error-shaped payload', { error: 'Configuration' }],
  ])('rejects %s', (_label, value) => {
    expect(hasSession(value)).toBe(false);
  });
});

describe('decide — protected routes', () => {
  it('sends an anonymous request to sign-in and remembers where it was going', () => {
    expect(decide('/dashboard', { authjs: null })).toEqual({
      action: 'redirect',
      to: '/sign-in',
      callbackUrl: '/dashboard',
    });
  });

  it('lets an authenticated request through', () => {
    expect(decide('/dashboard', { authjs: REAL_SESSION })).toEqual({ action: 'next' });
  });

  it.each(['/dashboard', '/transactions', '/receipts', '/upload', '/settings', '/ask', '/'])(
    'does not let a configuration error open %s',
    (route) => {
      // The fail-open, stated per route: this is what a bare `!request.auth`
      // check got wrong, and it got it wrong everywhere at once.
      expect(decide(route, { authjs: CONFIG_ERROR })).toEqual({
        action: 'redirect',
        to: '/sign-in',
        callbackUrl: route,
      });
    },
  );
});

describe('decide — the sign-in page', () => {
  it('stays public for anonymous visitors', () => {
    expect(decide('/sign-in', { authjs: null })).toEqual({ action: 'next' });
  });

  it('sends a signed-in visitor to the dashboard', () => {
    expect(decide('/sign-in', { authjs: REAL_SESSION })).toEqual({ action: 'redirect', to: '/dashboard' });
  });

  it('does not bounce a visitor off sign-in on a configuration error', () => {
    // The same bug from the other side. Treating the error object as a session
    // would redirect anonymous visitors away from the only page that could get
    // them a real one, locking everybody out of a misconfigured deployment.
    expect(decide('/sign-in', { authjs: CONFIG_ERROR })).toEqual({ action: 'next' });
  });

  it('keeps the callback URL of a deep link', () => {
    expect(decide('/sign-in/whatever', { authjs: null })).toEqual({ action: 'next' });
  });
});
