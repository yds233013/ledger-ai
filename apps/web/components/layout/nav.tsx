'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';

import { cn } from '@/lib/cn';

const LINKS = [
  { href: '/dashboard', label: 'Dashboard' },
  { href: '/upload', label: 'Upload' },
  { href: '/receipts', label: 'Receipts' },
  { href: '/transactions', label: 'Transactions' },
  { href: '/ask', label: 'Ask Ledger' },
  { href: '/settings', label: 'Settings' },
] as const;

export function Nav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Main" className="flex items-center gap-1">
      {LINKS.map((link) => {
        const active = pathname === link.href || pathname.startsWith(`${link.href}/`);
        return (
          <Link
            key={link.href}
            href={link.href}
            aria-current={active ? 'page' : undefined}
            className={cn(
              'rounded-lg px-3 py-1.5 text-sm font-medium transition-colors',
              active
                ? 'bg-brand-soft text-brand'
                : 'text-ink-muted hover:bg-surface-sunken hover:text-ink',
            )}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
