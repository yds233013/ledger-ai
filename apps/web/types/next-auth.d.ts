import 'next-auth';
import 'next-auth/jwt';

declare module 'next-auth' {
  interface Session {
    user: {
      id: string;
      email?: string | null;
      name?: string | null;
      /** True for an ephemeral per-visitor demo account. */
      isDemo?: boolean;
      /** ISO timestamp at which a demo account stops working. */
      demoExpiresAt?: string | null;
    };
  }

  /** Extra fields the demo provider returns from `authorize`. */
  interface User {
    isDemo?: boolean;
    demoExpiresAt?: string | null;
  }
}

declare module 'next-auth/jwt' {
  interface JWT {
    isDemo?: boolean;
    demoExpiresAt?: string | null;
  }
}
