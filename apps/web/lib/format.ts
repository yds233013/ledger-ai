/** Display formatting. All money arrives as integer cents from the API. */

const currency = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  minimumFractionDigits: 2,
});

const compact = new Intl.NumberFormat('en-US', {
  style: 'currency',
  currency: 'USD',
  notation: 'compact',
  maximumFractionDigits: 1,
});

export function formatCents(cents: number): string {
  return currency.format(cents / 100);
}

export function formatMoney(amount: number): string {
  return currency.format(amount);
}

/** Spending is stored as a negative amount; show it as a positive magnitude. */
export function formatSpend(cents: number): string {
  return currency.format(Math.abs(cents) / 100);
}

export function formatCompact(amount: number): string {
  return compact.format(amount);
}

export function formatPercent(value: number | null): string {
  if (value === null || Number.isNaN(value)) return '—';
  return `${value > 0 ? '+' : ''}${value.toFixed(1)}%`;
}

export function formatDate(iso: string): string {
  const [year, month, day] = iso.slice(0, 10).split('-').map(Number);
  return new Date(year, month - 1, day).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
  });
}

export function formatShortDate(iso: string): string {
  const [year, month, day] = iso.slice(0, 10).split('-').map(Number);
  return new Date(year, month - 1, day).toLocaleDateString('en-US', {
    month: 'short',
    day: 'numeric',
  });
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function formatDuration(ms: number): string {
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function confidenceLabel(confidence: number): {
  label: string;
  tone: 'high' | 'medium' | 'low';
} {
  if (confidence >= 0.9) return { label: 'High', tone: 'high' };
  if (confidence >= 0.6) return { label: 'Medium', tone: 'medium' };
  return { label: 'Low', tone: 'low' };
}
