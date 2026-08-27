import { Badge } from '@/components/ui/primitives';

/** Below this the backend routes a receipt to manual review. */
export const FIELD_REVIEW_THRESHOLD = 0.75;

export function FieldConfidence({ score }: { score: number | undefined }) {
  if (score === undefined) {
    return <Badge tone="neutral">not found</Badge>;
  }
  if (score >= 0.9) return <Badge tone="positive">high</Badge>;
  if (score >= FIELD_REVIEW_THRESHOLD) return <Badge tone="caution">medium</Badge>;
  return <Badge tone="negative">low — please check</Badge>;
}
