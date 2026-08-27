/**
 * Chart configuration shared by every visualization.
 *
 * Colours are CSS custom properties (see globals.css) rather than literals, so
 * light/dark swap happens in CSS with no JS and no flash. Recharts passes these
 * straight through to SVG fill/stroke, which accepts var().
 */
import type { ChartDatum } from './types';

/** Fixed categorical order. Never cycled — past MAX_SERIES we fold to "Other". */
export const SERIES = [
  'var(--viz-1)',
  'var(--viz-2)',
  'var(--viz-3)',
  'var(--viz-4)',
  'var(--viz-5)',
  'var(--viz-6)',
  'var(--viz-7)',
] as const;

export const MAX_SERIES = SERIES.length;

export const PRIMARY = SERIES[0];
export const MUTED = 'var(--viz-muted)';
export const GRID = 'var(--viz-grid)';
export const AXIS = 'var(--viz-axis)';
export const SURFACE = 'var(--viz-surface)';

/** Bar data-ends: 4px rounded, anchored to the baseline. */
export const BAR_RADIUS: [number, number, number, number] = [4, 4, 0, 0];
export const BAR_RADIUS_HORIZONTAL: [number, number, number, number] = [0, 4, 4, 0];

export const axisProps = {
  stroke: AXIS,
  tickLine: false,
  axisLine: false,
  style: { fontSize: 11 },
} as const;

export const gridProps = {
  stroke: GRID,
  strokeDasharray: '3 3',
  vertical: false,
} as const;

/**
 * Cap the number of slices and fold the tail into a single "Other" entry.
 * Beyond seven, categorical hues stop being distinguishable — generating an
 * eighth is the thing this prevents.
 */
export function foldToOther(data: ChartDatum[], max = MAX_SERIES): ChartDatum[] {
  if (data.length <= max) return data;
  const head = data.slice(0, max - 1);
  const tail = data.slice(max - 1);
  const otherValue = tail.reduce((sum, item) => sum + item.value, 0);
  return [
    ...head,
    {
      label: `Other (${tail.length})`,
      value: Math.round(otherValue * 100) / 100,
      color: MUTED,
    },
  ];
}

/** Truncate a long axis label without dropping its meaning entirely. */
export function truncateLabel(label: string, max = 18): string {
  return label.length <= max ? label : `${label.slice(0, max - 1)}…`;
}
