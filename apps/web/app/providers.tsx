'use client';

import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { SessionProvider } from 'next-auth/react';
import { useState, type ReactNode } from 'react';

import { ApiError } from '@/lib/api-client';
import { ClerkGate } from '@/components/auth/clerk-gate';

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30_000,
            refetchOnWindowFocus: false,
            retry: (failureCount, error) => {
              // A 4xx will not become a 2xx by asking again.
              if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
                return false;
              }
              return failureCount < 2;
            },
          },
        },
      }),
  );

  // Both providers are always mounted. SessionProvider carries the demo
  // session; ClerkGate mounts ClerkProvider only when a publishable key exists
  // and otherwise renders its children untouched, so an unconfigured
  // deployment behaves exactly as it did before Clerk was added.
  return (
    <ClerkGate>
      <SessionProvider>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </SessionProvider>
    </ClerkGate>
  );
}
