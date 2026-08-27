'use client';

import { useSession } from 'next-auth/react';
import { useEffect, useState } from 'react';

/**
 * Standing notice for an ephemeral demo account.
 *
 * A demo that simply stops working after 24 hours reads as a bug. Saying how
 * long is left, and that the data is synthetic and will be deleted, turns an
 * unexplained failure into an expected one.
 */
export function DemoBanner() {
  const { data: session } = useSession();
  const expiresAt = session?.user?.demoExpiresAt ?? null;
  const [now, setNow] = useState(() => Date.now());

  const isDemo = session?.user?.isDemo === true && Boolean(expiresAt);

  useEffect(() => {
    if (!isDemo) return;
    // A minute is enough resolution for a 24-hour countdown, and keeps this
    // from being a once-a-second re-render for the whole session.
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, [isDemo]);

  if (!isDemo || !expiresAt) return null;

  const remainingMs = new Date(expiresAt).getTime() - now;
  const expired = remainingMs <= 0;
  const hours = Math.floor(remainingMs / 3_600_000);
  const minutes = Math.floor((remainingMs % 3_600_000) / 60_000);

  const remaining = hours > 0 ? `${hours}h ${minutes}m` : `${Math.max(0, minutes)}m`;

  return (
    <div
      role="status"
      data-testid="demo-banner"
      className={`border-b px-4 py-2 sm:px-6 ${
        expired ? 'border-negative/30 bg-negative/5' : 'border-line bg-brand-soft'
      }`}
    >
      <p className="mx-auto max-w-7xl text-xs leading-relaxed text-ink-muted">
        <strong className="font-medium text-ink">
          {expired ? 'This demo has ended.' : 'Temporary demo account.'}
        </strong>{' '}
        {expired ? (
          <>
            Its data has been scheduled for deletion. Sign out and start a new demo to
            continue exploring.
          </>
        ) : (
          <>
            Every transaction here is synthetic. This account and everything in it are
            deleted automatically in <span className="tabular-nums">{remaining}</span>.
          </>
        )}
      </p>
    </div>
  );
}
