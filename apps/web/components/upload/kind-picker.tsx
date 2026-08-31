'use client';

/**
 * What a PDF is, asked rather than guessed.
 *
 * A statement and a receipt are the same file format wanting completely
 * different treatment, and both misroutes lose data quietly: a statement read
 * as a receipt collapses a month of transactions into one row, and a receipt
 * read as a statement finds no table and imports nothing. A heuristic that is
 * right most of the time still fails silently the rest of the time, so the
 * server refuses a PDF that does not say which it is.
 *
 * CSVs never reach this — there is nothing ambiguous about them.
 */
export function KindPicker({
  value,
  onChange,
  disabled,
}: {
  value: 'statement' | 'receipt';
  onChange: (kind: 'statement' | 'receipt') => void;
  disabled?: boolean;
}) {
  const options = [
    {
      key: 'statement' as const,
      label: 'Bank statement',
      hint: 'A monthly statement with a table of transactions',
    },
    {
      key: 'receipt' as const,
      label: 'Receipt',
      hint: 'A single purchase, read with OCR',
    },
  ];

  return (
    <fieldset className="mt-4 rounded-lg border border-line bg-surface-sunken p-4" disabled={disabled}>
      <legend className="px-1 text-xs font-medium text-ink">If you upload a PDF, what is it?</legend>
      <div className="mt-2 flex flex-col gap-2 sm:flex-row sm:gap-3">
        {options.map((option) => (
          <label
            key={option.key}
            className={`flex flex-1 cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2.5 ${
              value === option.key ? 'border-brand bg-surface' : 'border-line'
            } ${disabled ? 'cursor-not-allowed opacity-60' : ''}`}
          >
            <input
              type="radio"
              name="upload-kind"
              value={option.key}
              checked={value === option.key}
              onChange={() => onChange(option.key)}
              className="mt-0.5 size-4 shrink-0 accent-brand"
              data-testid={`kind-${option.key}`}
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium text-ink">{option.label}</span>
              <span className="mt-0.5 block text-xs leading-relaxed text-ink-muted">
                {option.hint}
              </span>
            </span>
          </label>
        ))}
      </div>
      <p className="mt-3 text-xs leading-relaxed text-ink-faint">
        Statements are read from the text inside the PDF, not from a picture of it. A scanned
        statement is refused — your bank&rsquo;s CSV export will import cleanly instead. The
        original PDF is deleted as soon as you confirm the import.
      </p>
    </fieldset>
  );
}
