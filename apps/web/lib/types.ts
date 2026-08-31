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
  /** Transactions the categorizer was unsure about. */
  review_count: number;
  /** Transactions carrying an open alert. Unrelated to review_count. */
  flagged_count: number;
  total_count: number;
}

export type JobStage =
  | 'queued'
  | 'extracting'
  | 'normalizing'
  | 'categorizing'
  | 'analyzing'
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

/* --- receipts ------------------------------------------------------------ */

export type ReceiptStatus = 'pending' | 'needs_review' | 'confirmed' | 'failed';

export interface ReceiptSummary {
  id: string;
  status: ReceiptStatus;
  merchant: string | null;
  posted_date: string | null;
  total_cents: number | null;
  total: number | null;
  currency: string;
  ocr_confidence: number;
  needs_review: boolean;
  page_count: number;
  original_filename: string;
  content_type: string;
  transaction_id: string | null;
  link_mode: 'created' | 'linked' | null;
  created_at: string;
}

export interface ReceiptDetail extends ReceiptSummary {
  subtotal_cents: number | null;
  tax_cents: number | null;
  tip_cents: number | null;
  field_confidence: Record<string, number>;
  parse_notes: Record<string, string>;
  raw_text: string;
  currency_warning: string | null;
  base_currency: string;
  categories: Category[];
  accounts: Account[];
  default_account_name: string;
}

export interface MatchSignal {
  name: string;
  detail: string;
  contribution: number;
}

export interface MatchCandidate {
  transaction_id: string;
  posted_date: string;
  merchant: string;
  amount_cents: number;
  amount: number;
  currency: string;
  account_id: string;
  account_name: string;
  category: string | null;
  source_upload_id: string | null;
  source_filename: string | null;
  score: number;
  signals: MatchSignal[];
}

export interface MatchCandidatesResponse {
  receipt_id: string;
  candidates: MatchCandidate[];
  note: string;
}

export interface ConfirmResponse {
  receipt_id: string;
  transaction_id: string;
  mode: 'create' | 'link';
  amount_cents: number;
  message: string;
}

/* --- alerts --------------------------------------------------------------- */

export type AlertType =
  | 'duplicate'
  | 'near_duplicate'
  | 'unusual_amount'
  | 'new_merchant'
  | 'large_for_merchant';

export type AlertSeverity = 'low' | 'medium' | 'high';
export type AlertStatus = 'open' | 'dismissed' | 'resolved';

export interface Alert {
  id: string;
  alert_type: AlertType;
  severity: AlertSeverity;
  severity_note: string;
  status: AlertStatus;
  message: string;
  evidence: Record<string, unknown>;
  created_at: string;
  transaction_id: string;
  transaction_merchant: string;
  transaction_date: string;
  transaction_amount: number;
  transaction_category?: string | null;
}

export interface AlertList {
  items: Alert[];
  open_count: number;
  dismissed_count: number;
  resolved_count: number;
  disclaimer: string;
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
  /** How many months the trend actually covers — not always 12. */
  trend_months: number;
  recent: RecentTransaction[];
  needs_review_count: number;
  account_count: number;
  earliest_transaction: string | null;
  latest_transaction: string | null;
  base_currency: string;
  excluded_currencies: Record<string, number>;
  currency_note: string | null;
  pending_receipt_count: number;
  alerts_enabled: boolean;
  open_alert_count: number;
  alerts_shown: number;
  alerts: Alert[];
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

export interface RefinementChip {
  key: string;
  label: string;
  description: string;
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
  refinements?: RefinementChip[];
  refined_from?: string | null;
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

export interface DeletionResult {
  dry_run: boolean;
  account_removed: boolean;
  total_rows: number;
  rows_by_table: Record<string, number>;
  /** Readable names for the keys of rows_by_table. */
  table_labels: Record<string, string>;
  /** What this operation deliberately keeps. */
  retained: string[];
  storage_objects_removed: number;
  cache_keys_removed: number;
  queued_jobs_cancelled: number;
  errors: string[];
  message: string;
}

export interface Profile {
  email: string;
  display_name: string;
  is_demo: boolean;
  /** True only for ephemeral per-visitor demo accounts. */
  is_ephemeral_demo: boolean;
  demo_expires_at: string | null;
  demo_notice: string | null;
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

/**
 * Which documents this account has accepted, and at which version.
 *
 * Versions rather than booleans: "accepted the terms" says nothing useful
 * without saying *which* terms. When a version changes, the previously accepted
 * one no longer matches the required one and the account is asked again.
 */
export interface ConsentState {
  /** Consent type -> the version currently required. */
  required: Record<string, string>;
  /** Consent type -> the latest version this account accepted. */
  accepted: Record<string, string>;
  /** Prerequisites not yet accepted at the current version. */
  missing: string[];
}

/**
 * This account's own consumption against each private-beta budget.
 *
 * `applies` is false for demo accounts, which are bounded by their 24-hour
 * expiry instead. Every number describes the requesting account only.
 */
export interface Usage {
  applies: boolean;
  resets_at: string | null;
  uploads_today: number;
  uploads_per_day: number;
  bytes_today: number;
  upload_bytes_per_day: number;
  stored_bytes: number;
  stored_bytes_limit: number;
  transaction_rows: number;
  transaction_rows_limit: number;
  receipts: number;
  receipts_limit: number;
  jobs_in_flight: number;
  concurrent_jobs_limit: number;
  max_upload_bytes: number;
}

/** One transaction read out of a statement PDF, awaiting confirmation. */
export interface StatementRow {
  id: string;
  source_page: number;
  posted_date: string;
  description: string;
  amount_cents: number;
  balance_cents: number | null;
  direction: 'debit' | 'credit';
  confidence: number;
  flags: string[];
  excluded: boolean;
  edited: boolean;
  duplicate_of_existing: boolean;
}

/**
 * A parsed statement waiting for review.
 *
 * Nothing here is in the ledger. A statement PDF carries no machine-readable
 * table, so every row is an inference until someone confirms it — which is why
 * this is a separate thing from a transaction rather than a flag on one.
 */
export interface StatementImport {
  id: string;
  status: 'parsing' | 'needs_review' | 'committed' | 'failed';
  page_count: number;
  table_pages: number;
  skipped_lines: number;
  period_start: string | null;
  period_end: string | null;
  currency: string | null;
  balance_chain_checked: boolean;
  balance_chain_ok: boolean;
  verified_pages: number;
  verified_mismatches: number;
  notes: string[];
  expires_at: string;
  committed_at: string | null;
  row_count: number;
  rows?: StatementRow[];
  message?: string | null;
}
