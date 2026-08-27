/** Shapes mirrored from the FastAPI response models. */

export interface Category {
  id: string;
  name: string;
  slug: string;
  color: string;
  icon: string;
}

export interface Account {
  id: string;
  name: string;
  institution: string;
  account_type: string;
  mask: string;
}

export interface Transaction {
  id: string;
  posted_date: string;
  amount_cents: number;
  amount: number;
  currency: string;
  merchant: string;
  merchant_key: string;
  raw_description: string;
  category: Category | null;
  confidence: number;
  categorized_by: string;
  needs_review: boolean;
  is_corrected: boolean;
  account_id: string;
  account_name: string;
  upload_id: string | null;
  created_at: string;
}

export interface CorrectionImpact {
  merchant: string;
  merchant_key: string;
  matching_count: number;
  affected_count: number;
  protected_count: number;
  already_correct_count: number;
  affected_ids: string[];
}

export interface TransactionUpdateResult {
  transaction: Transaction;
  applied_to_matching: boolean;
  impact: CorrectionImpact;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
  has_more: boolean;
}

export interface Facets {
  categories: Category[];
  accounts: Account[];
  merchants: string[];
  review_count: number;
  total_count: number;
}

export type JobStage =
  | 'queued'
  | 'extracting'
  | 'normalizing'
  | 'categorizing'
  | 'complete'
  | 'failed';

export interface ProcessingJob {
  id: string;
  upload_id: string;
  stage: JobStage;
  progress: number;
  rows_total: number;
  rows_imported: number;
  rows_skipped: number;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Upload {
  id: string;
  original_filename: string;
  kind: string;
  size_bytes: number;
  status: string;
  created_at: string;
  job: ProcessingJob | null;
  duplicate_of_existing?: boolean;
  message?: string | null;
}

export interface CategorySlice {
  label: string;
  slug: string;
  value: number;
  value_cents: number;
  color: string;
  transaction_count: number;
}

export interface TrendPoint {
  label: string;
  month: string;
  value: number;
  value_cents: number;
}

export interface RecentTransaction {
  id: string;
  posted_date: string;
  merchant: string;
  amount: number;
  amount_cents: number;
  category: string;
  color: string;
  needs_review: boolean;
}

export interface Dashboard {
  period_label: string;
  total_spend: number;
  total_spend_cents: number;
  previous_spend: number;
  previous_spend_cents: number;
  delta_cents: number;
  delta_pct: number | null;
  delta_direction: 'up' | 'down' | 'flat';
  transaction_count: number;
  total_income: number;
  total_income_cents: number;
  net_cents: number;
  by_category: CategorySlice[];
  trend: TrendPoint[];
  recent: RecentTransaction[];
  needs_review_count: number;
  account_count: number;
  earliest_transaction: string | null;
  latest_transaction: string | null;
  alerts_enabled: boolean;
  alerts_note: string;
}

/* --- Ask Ledger ---------------------------------------------------------- */

export type AnalysisStepName =
  | 'understand'
  | 'select'
  | 'aggregate'
  | 'visualize'
  | 'explain';

export type StepStatus = 'started' | 'completed' | 'failed';

export interface AnalysisStepEvent {
  seq: number;
  step: AnalysisStepName;
  status: StepStatus;
  title: string;
  payload: Record<string, unknown>;
  duration_ms: number;
}

export interface ChartDatum {
  label: string;
  value: number;
  count?: number;
  color?: string;
}

export interface ChartSpec {
  kind: 'bar' | 'line' | 'area' | 'pie' | 'none';
  data: ChartDatum[];
  x_key: string;
  y_key: string;
  y_label: string;
  x_label: string;
  title: string;
  value_format: 'currency' | 'number';
  stacked: boolean;
  colors: string[];
}

export interface GroupedRow {
  label: string;
  value: number;
  value_cents: number;
  transaction_count: number;
  key?: string;
  color?: string;
}

export interface Comparison {
  current: number;
  previous: number;
  current_cents: number;
  previous_cents: number;
  current_label: string;
  previous_label: string;
  delta: number;
  delta_cents: number;
  delta_pct: number | null;
  direction: 'up' | 'down' | 'flat';
}

export interface AnalysisResultData {
  total: number;
  total_cents: number;
  transaction_count: number;
  rows: GroupedRow[];
  comparison: Comparison | null;
  metric_label: string;
  caveats: string[];
}

export interface SupportingTransaction {
  id: string;
  posted_date: string;
  merchant: string;
  description: string;
  category: string;
  color: string;
  amount: number;
  amount_cents: number;
  needs_review: boolean;
}

export interface AnalysisResult {
  run_id: string;
  question?: string;
  plan?: Record<string, unknown> | null;
  result: AnalysisResultData | null;
  chart: ChartSpec | null;
  narration: string;
  supporting_transactions: SupportingTransaction[];
  caveats?: string[];
  planner?: string;
  narrator?: string;
  duration_ms?: number;
  cached: boolean;
  declined: boolean;
}

export interface Capabilities {
  ai_enabled: boolean;
  planner: string;
  narrator: string;
  disclosure: string;
  suggested_questions: string[];
}

export interface FeatureStatus {
  key: string;
  label: string;
  available: boolean;
  note: string;
}

export interface Profile {
  email: string;
  display_name: string;
  is_demo: boolean;
  transaction_count: number;
  account_count: number;
  upload_count: number;
  ai_enabled: boolean;
  ai_disclosure: string;
  features: FeatureStatus[];
}

export interface RunSummary {
  id: string;
  question: string;
  status: string;
  narration: string | null;
  duration_ms: number;
  created_at: string;
  cached: boolean;
}
