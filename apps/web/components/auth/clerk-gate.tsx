'use client';

import { ClerkProvider } from '@clerk/nextjs';
import type { ReactNode } from 'react';

import { clerkPublishableKey, clerkConfigured } from '@/lib/clerk-config';

/**
 * Mounts `ClerkProvider` only when Clerk is actually configured.
 *
 * `ClerkProvider` throws when constructed without a publishable key, and it
 * wraps the entire application — so an unconfigured deployment would fail to
 * render anything at all, including the demo. That is the single most important
 * thing this component prevents: the 24-hour demo must not depend on Clerk
 * being present, correct, or reachable.
 *
 * With no key it renders its children unchanged, which is exactly the tree the
 * application had before Clerk existed.
 */
export function ClerkGate({ children }: { children: ReactNode }) {
  if (!clerkConfigured) return <>{children}</>;

  return (
    <ClerkProvider
      publishableKey={clerkPublishableKey}
      // The beta sign-in lives at /beta; Clerk's own hosted pages are not used.
      signInUrl="/beta"
      signUpUrl="/beta"
      afterSignOutUrl="/sign-in"
    >
      {children}
    </ClerkProvider>
  );
}
