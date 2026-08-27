import { describe, expect, it } from 'vitest';

import {
  confidenceLabel,
  formatCents,
  formatDuration,
  formatPercent,
  formatSpend,
} from '@/lib/format';

describe('money formatting', () => {
  it('formats integer cents without float drift', () => {
    expect(formatCents(48273)).toBe('$482.73');
    expect(formatCents(10)).toBe('$0.10');
    expect(formatCents(0)).toBe('$0.00');
  });

  it('shows spending as a positive magnitude', () => {
    // Outflows are stored negative; the UI shows the size, with a sign glyph
    // supplied separately by the row.
    expect(formatSpend(-4500)).toBe('$45.00');
    expect(formatSpend(4500)).toBe('$45.00');
  });
});

describe('formatPercent', () => {
  it('signs positive changes and renders a dash for missing values', () => {
    expect(formatPercent(12.7)).toBe('+12.7%');
    expect(formatPercent(-12.7)).toBe('-12.7%');
    expect(formatPercent(null)).toBe('—');
  });
});

describe('formatDuration', () => {
  it('switches units at one second', () => {
    expect(formatDuration(48)).toBe('48 ms');
    expect(formatDuration(1500)).toBe('1.5 s');
  });
});

describe('confidenceLabel', () => {
  it('maps the documented confidence bands', () => {
    expect(confidenceLabel(1).label).toBe('High');
    expect(confidenceLabel(0.9).label).toBe('High');
    expect(confidenceLabel(0.65).label).toBe('Medium');
    // Below 0.6 is exactly the review-queue threshold in the backend.
    expect(confidenceLabel(0.5).label).toBe('Low');
    expect(confidenceLabel(0).label).toBe('Low');
  });
});
