import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';

export function Card({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={cn('card', className)}>{children}</div>;
}

export function CardHeader({
  title,
  subtitle,
  action,
}: {
  title: ReactNode;
  subtitle?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
      <div className="min-w-0">
        <h2 className="text-sm font-semibold text-ink">{title}</h2>
        {subtitle ? <p className="mt-0.5 text-xs text-ink-muted">{subtitle}</p> : null}
      </div>
      {action}
    </div>
  );
}

export function Badge({
  children,
  tone = 'neutral',
  className,
}: {
  children: ReactNode;
  tone?: 'neutral' | 'brand' | 'positive' | 'negative' | 'caution';
  className?: string;
}) {
  const tones = {
    neutral: 'bg-surface-sunken text-ink-muted',
    brand: 'bg-brand-soft text-brand',
    positive: 'bg-positive/10 text-positive',
    negative: 'bg-negative/10 text-negative',
    caution: 'bg-caution/10 text-caution',
  } as const;
  return <span className={cn('chip', tones[tone], className)}>{children}</span>;
}

export function Skeleton({ className }: { className?: string }) {
  return (
    <div
      className={cn('relative overflow-hidden rounded-md bg-surface-sunken', className)}
      aria-hidden="true"
    >
      <div className="absolute inset-0 -translate-x-full animate-shimmer bg-gradient-to-r from-transparent via-black/5 to-transparent dark:via-white/5" />
    </div>
  );
}

export function EmptyState({
  title,
  description,
  action,
  icon,
}: {
  title: string;
  description: string;
  action?: ReactNode;
  icon?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      {icon ? <div className="mb-3 text-ink-faint">{icon}</div> : null}
      <p className="text-sm font-medium text-ink">{title}</p>
      <p className="mt-1 max-w-sm text-sm text-ink-muted">{description}</p>
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  );
}

export function ErrorState({
  title = 'Something went wrong',
  message,
  onRetry,
}: {
  title?: string;
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-col items-center justify-center px-6 py-12 text-center"
    >
      <p className="text-sm font-medium text-negative">{title}</p>
      <p className="mt-1 max-w-md text-sm text-ink-muted">{message}</p>
      {onRetry ? (
        <button type="button" onClick={onRetry} className="btn-secondary mt-4">
          Try again
        </button>
      ) : null}
    </div>
  );
}

/**
 * Marks anything an AI model touched. In Phase 1 no model is configured, so
 * this renders the deterministic-engine variant — the disclosure always
 * reflects what actually ran.
 */
export function AiBadge({ aiEnabled }: { aiEnabled: boolean }) {
  return (
    <Badge tone={aiEnabled ? 'brand' : 'neutral'}>
      {aiEnabled ? 'AI-assisted' : 'Deterministic engine'}
    </Badge>
  );
}

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={cn('h-4 w-4 animate-spin', className)}
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden="true"
    >
      <circle className="opacity-20" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path
        className="opacity-90"
        fill="currentColor"
        d="M4 12a8 8 0 0 1 8-8v4a4 4 0 0 0-4 4H4z"
      />
    </svg>
  );
}
