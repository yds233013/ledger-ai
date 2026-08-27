'use client';

import { useCallback, useRef, useState } from 'react';

import { cn } from '@/lib/cn';
import { formatBytes } from '@/lib/format';

const MAX_BYTES = 10 * 1024 * 1024;
const ACCEPTED = '.csv,.txt,.png,.jpg,.jpeg,.webp';

/** Client-side pre-check. The server validates independently and is the
 *  authority — this only saves the user a round trip. */
function preValidate(file: File): string | null {
  if (file.size === 0) return 'That file is empty.';
  if (file.size > MAX_BYTES) {
    return `${formatBytes(file.size)} exceeds the 10 MB limit.`;
  }
  const name = file.name.toLowerCase();
  const ok = ['.csv', '.txt', '.png', '.jpg', '.jpeg', '.webp'].some((ext) =>
    name.endsWith(ext),
  );
  if (!ok) return 'Only .csv statements and .png/.jpg/.webp receipt images are accepted.';
  return null;
}

export function Dropzone({
  onFiles,
  disabled = false,
}: {
  onFiles: (files: File[]) => void;
  disabled?: boolean;
}) {
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const handle = useCallback(
    (fileList: FileList | null) => {
      setError(null);
      if (!fileList?.length) return;

      const files = Array.from(fileList);
      const problems = files.map(preValidate).filter(Boolean);
      if (problems.length) {
        setError(problems[0] as string);
        return;
      }
      onFiles(files);
    },
    [onFiles],
  );

  return (
    <div>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        aria-label="Upload a statement or receipt"
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(event) => {
          if (!disabled && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (!disabled) handle(event.dataTransfer.files);
        }}
        className={cn(
          'flex flex-col items-center justify-center rounded-xl border-2 border-dashed px-6 py-12 text-center transition-colors',
          disabled
            ? 'cursor-not-allowed border-line opacity-60'
            : 'cursor-pointer border-line hover:border-brand hover:bg-brand-soft/40',
          dragging && 'border-brand bg-brand-soft/60',
        )}
      >
        <span aria-hidden="true" className="text-2xl">
          ⬆
        </span>
        <p className="mt-3 text-sm font-medium text-ink">
          Drop a CSV statement here, or click to choose
        </p>
        <p className="mt-1 text-xs text-ink-muted">
          CSV up to 10 MB. Receipt images are accepted but OCR arrives in Phase 2.
        </p>
      </div>

      <input
        ref={inputRef}
        type="file"
        accept={ACCEPTED}
        multiple
        className="sr-only"
        onChange={(event) => {
          handle(event.target.files);
          event.target.value = '';
        }}
      />

      {error ? (
        <p role="alert" className="mt-3 text-sm text-negative">
          {error}
        </p>
      ) : null}
    </div>
  );
}
