'use client';

import type { RefinementChip } from '@/lib/types';

/**
 * Follow-up questions as explicit plan refinements.
 *
 * Each chip is a named, deterministic transformation of the plan that produced
 * this answer — no pronouns, no carried conversation. The refined plan is
 * re-validated and shown in the understanding step exactly as a fresh question
 * would be, so the user can always see what was asked on their behalf.
 */
export function RefinementChips({
  chips,
  onRefine,
  disabled = false,
}: {
  chips: RefinementChip[];
  onRefine: (chip: RefinementChip) => void;
  disabled?: boolean;
}) {
  if (chips.length === 0) return null;

  return (
    <div className="border-t border-line pt-4">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-ink-muted">
        Follow up
      </p>
      <div className="flex flex-wrap gap-2">
        {chips.map((chip) => (
          <button
            key={chip.key}
            type="button"
            disabled={disabled}
            title={chip.description}
            onClick={() => onRefine(chip)}
            className="rounded-full border border-line px-3 py-1.5 text-xs text-ink-muted transition-colors hover:border-brand hover:bg-brand-soft hover:text-brand disabled:cursor-not-allowed disabled:opacity-50"
          >
            {chip.label}
          </button>
        ))}
      </div>
      <p className="mt-2 text-xs text-ink-faint">
        Each follow-up is a fixed change to the query above — nothing is inferred from
        conversation history.
      </p>
    </div>
  );
}
