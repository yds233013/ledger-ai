/**
 * Missing Clerk configuration must not break anything.
 *
 * `ClerkProvider` and `clerkMiddleware` both throw without a publishable key,
 * and both sit on the path of every request. An unconfigured deployment — which
 * is the state this ships in, and the rollback position — therefore has to
 * render and route exactly as it did before Clerk was added.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const KEY = 'NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY';

async function loadConfig(value: string | undefined) {
  vi.resetModules();
  if (value === undefined) delete process.env[KEY];
  else process.env[KEY] = value;
  return import('@/lib/clerk-config');
}

describe('clerk-config', () => {
  const original = process.env[KEY];

  beforeEach(() => {
    delete process.env[KEY];
  });

  afterEach(() => {
    if (original === undefined) delete process.env[KEY];
    else process.env[KEY] = original;
    vi.resetModules();
  });

  it('reports not-configured when the key is absent', async () => {
    const config = await loadConfig(undefined);
    expect(config.clerkConfigured).toBe(false);
    expect(config.clerkPublishableKey).toBe('');
  });

  it('reports not-configured when the key is empty', async () => {
    const config = await loadConfig('');
    expect(config.clerkConfigured).toBe(false);
  });

  it('reports configured when a key is present', async () => {
    const config = await loadConfig('pk_live_' + 'a'.repeat(32));
    expect(config.clerkConfigured).toBe(true);
    expect(config.clerkIsProduction).toBe(true);
  });

  it('distinguishes a development key from a production one', async () => {
    const config = await loadConfig('pk_test_' + 'a'.repeat(32));
    expect(config.clerkConfigured).toBe(true);
    expect(config.clerkIsProduction).toBe(false);
  });

  it('never throws on a malformed key', async () => {
    // Whatever is in the environment, importing this module must not explode:
    // it is imported by the root layout.
    const config = await loadConfig('not-a-key');
    expect(config.clerkConfigured).toBe(true);
    expect(config.clerkIsProduction).toBe(false);
  });
});

describe('routing without Clerk', () => {
  it('behaves exactly as the demo-only application did', async () => {
    const { decide } = await import('@/lib/route-access');

    // No Clerk signal at all: the pre-Clerk contract, unchanged.
    expect(decide('/dashboard', {})).toEqual({
      action: 'redirect',
      to: '/sign-in',
      callbackUrl: '/dashboard',
    });
    expect(decide('/dashboard', { authjs: { user: { id: 'demo-1' } } })).toEqual({
      action: 'next',
    });
    expect(decide('/sign-in', {})).toEqual({ action: 'next' });
    expect(decide('/upload', { authjs: { user: { id: 'demo-1' } } })).toEqual({
      action: 'next',
    });
  });
});
