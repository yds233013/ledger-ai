/**
 * Auth.js configuration.
 *
 * Three ways in, and only the first is required for the app to work:
 *
 *   1. Credentials — verification is delegated to FastAPI, so password hashing
 *      lives in exactly one place. This is the local development path.
 *   2. Demo — provisions a fresh, isolated, expiring demo account server-side
 *      and signs the visitor straight into it. No password is involved and
 *      none is ever sent to the browser.
 *   3. GitHub — OPTIONAL, for a persistent account. Registered only when both
 *      AUTH_GITHUB_ID and AUTH_GITHUB_SECRET are present, so a clone with no
 *      OAuth application still runs, still signs in, and still demos.
 *
 * All three end at the same contract: an Auth.js session whose `user.id` is a
 * Ledger AI user id. /api/auth/token mints the short-lived HS256 bearer token
 * from that id, and FastAPI verifies it with the shared AUTH_SECRET.
 */
import NextAuth, { type NextAuthConfig } from 'next-auth';
import Credentials from 'next-auth/providers/credentials';
import GitHub from 'next-auth/providers/github';

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

/**
 * GitHub is registered only when real values are present.
 *
 * Decided at module scope rather than inside the provider so an unconfigured
 * deployment shows no GitHub button at all, instead of one that fails after
 * the user has already clicked it.
 */
export const githubEnabled = Boolean(
  process.env.AUTH_GITHUB_ID && process.env.AUTH_GITHUB_SECRET,
);

/**
 * Ask the API to provision an ephemeral demo account.
 *
 * The visitor's address is forwarded so the API's demo rate limit counts per
 * visitor rather than per web container. The API believes that header only
 * when its socket peer is inside its own TRUSTED_PROXY_IPS allow-list, so
 * sending it is safe: an unconfigured deployment ignores it and falls back to
 * the socket peer.
 */
async function provisionDemoAccount(forwardedFor: string | null) {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (forwardedFor) headers['X-Forwarded-For'] = forwardedFor;

  const response = await fetch(`${API_URL}/api/auth/demo-session`, {
    method: 'POST',
    headers,
    body: JSON.stringify({}),
    cache: 'no-store',
  });

  if (!response.ok) {
    // The upstream body can describe dependency state, so only the status code
    // is logged and the user sees a generic failure.
    console.error('Demo provisioning failed with status', response.status);
    return null;
  }

  const data = await response.json();
  return {
    id: data.user.id as string,
    email: data.user.email as string,
    name: data.user.display_name as string,
    isDemo: true as const,
    demoExpiresAt: (data.demo_expires_at as string) ?? null,
  };
}

const providers: NextAuthConfig['providers'] = [
  Credentials({
    id: 'credentials',
    name: 'Email and password',
    credentials: {
      email: { label: 'Email', type: 'email' },
      password: { label: 'Password', type: 'password' },
    },
    async authorize(credentials) {
      if (!credentials?.email || !credentials?.password) return null;

      const response = await fetch(`${API_URL}/api/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email: String(credentials.email),
          password: String(credentials.password),
        }),
      });

      if (!response.ok) return null;
      const data = await response.json();
      return {
        id: data.user.id,
        email: data.user.email,
        name: data.user.display_name,
      };
    },
  }),

  /**
   * The "Try the demo" button.
   *
   * Modelled as a Credentials provider that takes no credentials, because the
   * whole exchange has to happen server-side: the browser never learns an
   * address or a password for the account it is signed into, so a demo account
   * cannot be re-entered, shared or guessed at afterwards.
   */
  Credentials({
    id: 'demo',
    name: 'Demo account',
    credentials: {},
    async authorize(_credentials, request) {
      const forwardedFor = request?.headers?.get('x-forwarded-for') ?? null;
      return await provisionDemoAccount(forwardedFor);
    },
  }),
];

if (githubEnabled) {
  providers.push(
    GitHub({
      clientId: process.env.AUTH_GITHUB_ID,
      clientSecret: process.env.AUTH_GITHUB_SECRET,
      /**
       * Only what is needed to identify the account — no repository,
       * organisation or gist access — so authorising Ledger AI grants it
       * nothing beyond a stable identity and an address.
       */
      authorization: { params: { scope: 'read:user user:email' } },
    }),
  );
}

export const { handlers, signIn, signOut, auth } = NextAuth({
  session: { strategy: 'jwt', maxAge: 60 * 60 * 8 },
  pages: { signIn: '/sign-in', error: '/sign-in' },
  trustHost: true,
  providers,
  callbacks: {
    /**
     * Account resolution for GitHub.
     *
     * A GitHub identity is resolved to a Ledger AI account by the API, keyed on
     * the provider's account id. Matching on the email address instead would be
     * an account-takeover route: GitHub will report an address it has not
     * verified, and anyone able to set theirs to a known user's would inherit
     * that user's data. The verified flag is passed through and the API decides;
     * this layer never links on an unverified address.
     */
    async signIn({ account, profile }) {
      if (account?.provider !== 'github') return true;

      const verified = (profile as { email_verified?: boolean } | null)?.email_verified;
      const response = await fetch(`${API_URL}/api/auth/oauth/github`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          provider_account_id: String(account.providerAccountId),
          email: profile?.email ?? null,
          email_verified: verified === true,
          display_name: (profile?.name as string | undefined) ?? null,
        }),
        cache: 'no-store',
      });

      if (!response.ok) {
        // No token, no callback parameter, no provider payload — a status code
        // is the most that may reach a log line here.
        console.error('GitHub account resolution failed with status', response.status);
        return false;
      }

      const data = await response.json();
      // Read by the jwt callback below, which is what puts the Ledger AI user
      // id — never the GitHub id — into the session.
      (account as { ledgeraiUserId?: string }).ledgeraiUserId = data.user.id;
      return true;
    },

    async jwt({ token, user, account }) {
      const linked = (account as { ledgeraiUserId?: string } | null)?.ledgeraiUserId;
      if (linked) token.sub = linked;
      else if (user?.id) token.sub = user.id;

      const demoUser = user as { isDemo?: boolean; demoExpiresAt?: string | null } | undefined;
      if (demoUser?.isDemo) {
        token.isDemo = true;
        token.demoExpiresAt = demoUser.demoExpiresAt ?? null;
      }
      return token;
    },

    async session({ session, token }) {
      if (token.sub && session.user) session.user.id = token.sub;
      if (session.user) {
        session.user.isDemo = token.isDemo === true;
        session.user.demoExpiresAt = (token.demoExpiresAt as string | null) ?? null;
      }
      return session;
    },
  },
});
