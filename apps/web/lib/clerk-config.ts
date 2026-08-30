/**
 * Whether Clerk is configured for this deployment, in one place.
 *
 * `NEXT_PUBLIC_*` values are inlined at build time, so this must read the
 * variable literally rather than through a computed key — a dynamic lookup
 * would come back undefined in the browser bundle no matter what the
 * environment holds.
 *
 * Configured means "a publishable key exists". It deliberately says nothing
 * about whether persistent sign-in will succeed: the API decides that, from
 * CLERK_ENABLED and a matching local invitation. The frontend can be fully
 * wired while the backend still refuses every Clerk token, which is exactly
 * the state this ships in.
 */
const publishableKey = process.env.NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY ?? '';

export const clerkPublishableKey = publishableKey;
export const clerkConfigured = publishableKey.length > 0;

/**
 * Production keys are `pk_live_`, development keys `pk_test_`. Surfaced so the
 * sign-in page can avoid implying a production beta when pointed at a
 * development instance.
 */
export const clerkIsProduction = publishableKey.startsWith('pk_live_');
