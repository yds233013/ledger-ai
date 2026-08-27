'use client';

import { useEffect, useState } from 'react';

import { Spinner } from '@/components/ui/primitives';
import { API_URL, fetchAuthorizedObjectUrl } from '@/lib/api-client';
import { cn } from '@/lib/cn';

/**
 * The stored receipt, fetched through the authorized endpoint.
 *
 * The image is never a public URL: it is fetched with the bearer token and
 * held as an object URL for the life of the component. A PDF is rasterized
 * server-side to PNG, so the browser is never asked to render an untrusted PDF.
 */
export function ReceiptImage({
  receiptId,
  pageCount,
  isPdf,
}: {
  receiptId: string;
  pageCount: number;
  isPdf: boolean;
}) {
  const [page, setPage] = useState(1);
  const [src, setSrc] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [rotation, setRotation] = useState(0);
  const [zoomed, setZoomed] = useState(false);

  useEffect(() => {
    let objectUrl: string | null = null;
    let cancelled = false;

    setSrc(null);
    setError(null);

    fetchAuthorizedObjectUrl(`/api/receipts/${receiptId}/image?page=${page}`)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setSrc(url);
      })
      .catch((cause: unknown) =>
        setError(cause instanceof Error ? cause.message : 'Could not load this receipt.'),
      );

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [receiptId, page]);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          onClick={() => setRotation((value) => (value + 90) % 360)}
          className="btn-secondary"
        >
          Rotate
        </button>
        <button
          type="button"
          onClick={() => setZoomed((value) => !value)}
          className="btn-secondary"
        >
          {zoomed ? 'Fit' : 'Zoom'}
        </button>

        {isPdf && pageCount > 1 ? (
          <span className="ml-auto flex items-center gap-2 text-xs text-ink-muted">
            <button
              type="button"
              disabled={page <= 1}
              onClick={() => setPage((value) => value - 1)}
              className="btn-ghost"
            >
              ‹
            </button>
            Page {page} of {pageCount}
            <button
              type="button"
              disabled={page >= pageCount}
              onClick={() => setPage((value) => value + 1)}
              className="btn-ghost"
            >
              ›
            </button>
          </span>
        ) : null}
      </div>

      <div
        className={cn(
          'flex items-center justify-center overflow-auto rounded-lg border border-line bg-surface-sunken p-3',
          zoomed ? 'max-h-[70vh]' : 'max-h-[52vh]',
        )}
      >
        {error ? (
          <p role="alert" className="p-6 text-sm text-negative">
            {error}
          </p>
        ) : src ? (
          /* eslint-disable-next-line @next/next/no-img-element -- object URL from
             an authorized fetch; next/image cannot carry the bearer token. */
          <img
            src={src}
            alt="The uploaded receipt"
            style={{ transform: `rotate(${rotation}deg)` }}
            className={cn(
              'origin-center transition-transform',
              zoomed ? 'max-w-none' : 'max-h-full max-w-full object-contain',
            )}
          />
        ) : (
          <div className="flex items-center gap-2 p-10 text-sm text-ink-muted">
            <Spinner /> Loading the receipt…
          </div>
        )}
      </div>

      <p className="text-xs text-ink-faint">
        Served from {new URL(API_URL).host} with your session only — this file has no
        public link.
      </p>
    </div>
  );
}
