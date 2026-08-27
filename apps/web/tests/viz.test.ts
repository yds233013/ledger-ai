import { describe, expect, it } from 'vitest';

import { MAX_SERIES, SERIES, foldToOther, truncateLabel } from '@/lib/viz';

describe('foldToOther', () => {
  const rows = (count: number) =>
    Array.from({ length: count }, (_, index) => ({
      label: `cat-${index}`,
      value: 10,
    }));

  it('leaves a palette-sized set untouched', () => {
    expect(foldToOther(rows(MAX_SERIES))).toHaveLength(MAX_SERIES);
  });

  it('folds the tail rather than inventing an eighth hue', () => {
    const result = foldToOther(rows(12));
    expect(result).toHaveLength(MAX_SERIES);
    expect(result[result.length - 1].label).toBe('Other (6)');
  });

  it('preserves the total when folding', () => {
    const input = rows(12);
    const before = input.reduce((sum, item) => sum + item.value, 0);
    const after = foldToOther(input).reduce((sum, item) => sum + item.value, 0);
    expect(after).toBeCloseTo(before, 2);
  });
});

describe('palette', () => {
  it('exposes exactly the validated slot count', () => {
    expect(SERIES).toHaveLength(7);
  });

  it('assigns slots in a fixed order', () => {
    // Colour follows the entity's position in the fixed order, never a
    // generated hue — regenerating must not change slot 1.
    expect(SERIES[0]).toBe('var(--viz-1)');
  });
});

describe('truncateLabel', () => {
  it('leaves short labels alone and ellipsizes long ones', () => {
    expect(truncateLabel('Groceries')).toBe('Groceries');
    expect(truncateLabel('A very long merchant name indeed', 12)).toBe('A very long…');
  });
});
