/**
 * Auth.js configuration.
 *
 * Phase 1 authenticates a seeded demo user through a Credentials provider that
 * delegates verification to the FastAPI backend, so password hashing lives in
 * exactly one place. Phase 3 adds OAuth providers here without any backend
 * change — the token contract (HS256, shared AUTH_SECRET) stays the same.
 */
import NextAuth from 'next-auth';
import Credentials from 'next-auth/providers/credentials';

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000';

export const { handlers, signIn, signOut, auth } = NextAuth({
  session: { strategy: 'jwt', maxAge: 60 * 60 * 8 },
  pages: { signIn: '/sign-in' },
  trustHost: true,
  providers: [
    Credentials({
      name: 'Demo account',
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
  ],
  callbacks: {
    async jwt({ token, user }) {
      if (user?.id) token.sub = user.id;
      return token;
    },
    async session({ session, token }) {
      if (token.sub && session.user) session.user.id = token.sub;
      return session;
    },
  },
});
