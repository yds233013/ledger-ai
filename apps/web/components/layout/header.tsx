'use client';

import { signOut, useSession } from 'next-auth/react';

import { clearTokenCache } from '@/lib/api-client';

import { Nav } from './nav';

export function Header() {
  const { data: session } = useSession();

  return (
    <header className="sticky top-0 z-30 border-b border-line bg-surface/85 backdrop-blur">
      <div className="mx-auto flex h-14 max-w-7xl items-center gap-6 px-4 sm:px-6">
        <div className="flex items-center gap-2">
          <span
            aria-hidden="true"
            className="grid h-7 w-7 place-items-center rounded-lg bg-brand text-sm font-bold text-white"
          >
            L
          </span>
          <span className="text-sm font-semibold tracking-tight">Ledger AI</span>
        </div>

        <div className="hidden md:block">
          <Nav />
        </div>

        <div className="ml-auto flex items-center gap-3">
          <span className="hidden text-xs text-ink-muted sm:inline">
            {session?.user?.email ?? ''}
          </span>
          <button
            type="button"
            className="btn-ghost"
            onClick={() => {
              clearTokenCache();
              void signOut({ callbackUrl: '/sign-in' });
            }}
          >
            Sign out
          </button>
        </div>
      </div>

      <div className="border-t border-line px-4 py-2 md:hidden">
        <Nav />
      </div>
    </header>
  );
}
