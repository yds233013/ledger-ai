import { Badge } from '@/components/ui/primitives';
import { confidenceLabel } from '@/lib/format';

const SOURCE_EXPLANATION: Record<string, string> = {
  correction: 'You corrected this merchant before',
  rule: 'Matched a known merchant pattern',
  keyword: 'Matched a keyword in the description',
  heuristic: 'Inferred from the amount, with no merchant match',
  llm: 'Suggested by a language model',
  none: 'No rule matched — needs your review',
};

/**
 * Confidence is shown as a word plus a source explanation, never as a bare
 * colour: the user should be able to see *why* something was categorized.
 */
export function ConfidenceIndicator({
  confidence,
  source,
}: {
  confidence: number;
  source: string;
}) {
  const { label, tone } = confidenceLabel(confidence);
  const badgeTone = tone === 'high' ? 'positive' : tone === 'medium' ? 'caution' : 'negative';

  return (
    <span
      className="inline-flex items-center gap-1.5"
      title={`${SOURCE_EXPLANATION[source] ?? source} (confidence ${confidence.toFixed(2)})`}
    >
      <Badge tone={badgeTone}>{label}</Badge>
    </span>
  );
}

export { SOURCE_EXPLANATION };
