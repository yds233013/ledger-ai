'use client';

import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';

import { formatCompact } from '@/lib/format';
import type { ChartDatum, ChartSpec } from '@/lib/types';
import {
  AXIS,
  BAR_RADIUS,
  MUTED,
  PRIMARY,
  SERIES,
  SURFACE,
  axisProps,
  foldToOther,
  gridProps,
  truncateLabel,
} from '@/lib/viz';

import { ChartTooltip } from './tooltip';

const HEIGHT = 260;

function moneyTick(value: number): string {
  return formatCompact(value);
}

/**
 * A single measure across labelled categories. One colour, deliberately: the
 * axis already names every bar, so per-bar colour would encode nothing and
 * would burn the categorical palette on a chart that doesn't need it.
 */
export function CategoryBarChart({
  data,
  valueFormat = 'currency',
}: {
  data: ChartDatum[];
  valueFormat?: 'currency' | 'number';
}) {
  const rows = foldToOther(data).map((item) => ({
    ...item,
    short: truncateLabel(item.label),
  }));

  return (
    <ResponsiveContainer width="100%" height={Math.max(HEIGHT, rows.length * 34)}>
      <BarChart data={rows} layout="vertical" margin={{ left: 8, right: 28, top: 4, bottom: 4 }}>
        <CartesianGrid {...gridProps} vertical horizontal={false} />
        <XAxis type="number" {...axisProps} tickFormatter={moneyTick} />
        <YAxis
          type="category"
          dataKey="short"
          width={124}
          {...axisProps}
          interval={0}
        />
        <Tooltip
          cursor={{ fill: 'rgb(148 163 184 / 0.12)' }}
          content={<ChartTooltip valueFormat={valueFormat} />}
        />
        <Bar
          dataKey="value"
          fill={PRIMARY}
          radius={[0, 4, 4, 0]}
          maxBarSize={18}
          isAnimationActive={false}
        />
      </BarChart>
    </ResponsiveContainer>
  );
}

/** Spending over time. One series, 2px stroke, 8px markers. */
export function TrendLineChart({
  data,
  valueFormat = 'currency',
}: {
  data: ChartDatum[];
  valueFormat?: 'currency' | 'number';
}) {
  return (
    <ResponsiveContainer width="100%" height={HEIGHT}>
      <LineChart data={data} margin={{ left: 4, right: 12, top: 8, bottom: 4 }}>
        <CartesianGrid {...gridProps} />
        <XAxis dataKey="label" {...axisProps} />
        <YAxis {...axisProps} tickFormatter={moneyTick} width={56} />
        <Tooltip
          cursor={{ stroke: AXIS, strokeDasharray: '3 3' }}
          content={<ChartTooltip valueFormat={valueFormat} />}
        />
        <Line
          type="monotone"
          dataKey="value"
          stroke={PRIMARY}
          strokeWidth={2}
          isAnimationActive={false}
          dot={{ r: 4, fill: PRIMARY, stroke: SURFACE, strokeWidth: 2 }}
          activeDot={{ r: 6, fill: PRIMARY, stroke: SURFACE, strokeWidth: 2 }}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

/** Two periods of the same measure. Still one measure, so still one colour. */
export function ComparisonBarChart({
  data,
  valueFormat = 'currency',
}: {
  data: ChartDatum[];
  valueFormat?: 'currency' | 'number';
}) {
  return (
    <ResponsiveContainer width="100%" height={HEIGHT}>
      <BarChart data={data} margin={{ left: 4, right: 12, top: 16, bottom: 4 }}>
        <CartesianGrid {...gridProps} />
        <XAxis dataKey="label" {...axisProps} />
        <YAxis {...axisProps} tickFormatter={moneyTick} width={56} />
        <Tooltip
          cursor={{ fill: 'rgb(148 163 184 / 0.12)' }}
          content={<ChartTooltip valueFormat={valueFormat} />}
        />
        <Bar dataKey="value" radius={BAR_RADIUS} maxBarSize={72} isAnimationActive={false}>
          {data.map((item, index) => (
            // The earlier period is recessive; the current period is the subject.
            <Cell key={item.label} fill={index === data.length - 1 ? PRIMARY : MUTED} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

/**
 * Part-to-whole. This is the one form where colour must carry identity, so it
 * uses the validated categorical order and is capped at seven slices.
 */
export function SharePieChart({ data }: { data: ChartDatum[] }) {
  const rows = foldToOther(data);

  return (
    <ResponsiveContainer width="100%" height={HEIGHT + 40}>
      <PieChart>
        <Pie
          data={rows}
          dataKey="value"
          nameKey="label"
          innerRadius={54}
          outerRadius={92}
          paddingAngle={2}
          stroke={SURFACE}
          strokeWidth={2}
          isAnimationActive={false}
        >
          {rows.map((item, index) => (
            <Cell key={item.label} fill={SERIES[index % SERIES.length]} />
          ))}
        </Pie>
        <Tooltip content={<ChartTooltip />} />
        <Legend
          verticalAlign="bottom"
          height={44}
          iconType="circle"
          iconSize={8}
          formatter={(value: string) => (
            <span className="text-xs text-ink-muted">{truncateLabel(value, 22)}</span>
          )}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}

/** Render whatever the backend's ChartSpec asked for. */
export function AnalysisChart({ spec }: { spec: ChartSpec }) {
  if (spec.kind === 'none' || spec.data.length === 0) return null;

  const isComparison = spec.data.length === 2 && spec.title.toLowerCase().includes(' vs ');

  return (
    <figure className="w-full">
      {spec.title ? (
        <figcaption className="mb-3 text-sm font-medium text-ink">{spec.title}</figcaption>
      ) : null}

      {spec.kind === 'pie' ? (
        <SharePieChart data={spec.data} />
      ) : spec.kind === 'line' || spec.kind === 'area' ? (
        <TrendLineChart data={spec.data} valueFormat={spec.value_format} />
      ) : isComparison ? (
        <ComparisonBarChart data={spec.data} valueFormat={spec.value_format} />
      ) : (
        <CategoryBarChart data={spec.data} valueFormat={spec.value_format} />
      )}

      {spec.y_label ? (
        <p className="mt-2 text-xs text-ink-faint">{spec.y_label}</p>
      ) : null}
    </figure>
  );
}
