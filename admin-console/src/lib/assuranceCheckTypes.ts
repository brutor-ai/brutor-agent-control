/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Continuous Assurance Checks + weekly digest —
 * wire types for /v1/admin/assurance-checks/* and
 * /v1/admin/assurance-digest/*.
 *
 * NL is authoring UX only: the runtime artifact is the versioned,
 * hash-pinned spec. Deterministic expressions are compile-checked by the
 * Rust core before they can be saved.
 */

export type CheckTier = 'deterministic' | 'semantic';
export type CheckOutput =
  | 'boolean'
  | 'classification'
  | 'score'
  | 'count'
  | 'extraction';
export type CheckTrigger = 'on_finalize' | 'batch';
export type ThresholdKind = 'value' | 'rate_over';
export type ThresholdOp = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte';

export const CHECK_OUTPUTS: CheckOutput[] = [
  'boolean',
  'classification',
  'score',
  'count',
  'extraction',
];

export const THRESHOLD_OPS: ThresholdOp[] = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte'];

export interface CheckThreshold {
  kind: ThresholdKind;
  op: ThresholdOp;
  value: unknown;
  severity: 'info' | 'warning' | 'critical';
  window_hours?: number | null;
}

/** deterministic: {expression}; semantic: {rubric, sample_rate, …}. */
export interface CheckEvaluator {
  expression?: string;
  rubric?: string;
  model_ref?: string | null;
  sample_rate?: number;
  max_tokens?: number;
  daily_token_budget?: number | null;
  labels?: string[];
}

export interface AssuranceCheck {
  id: string;
  tenant_id: string;
  name: string;
  description_nl: string | null;
  version: number;
  spec_hash: string;
  scope: { ai_system_ids?: string[] };
  tier: CheckTier;
  evaluator: CheckEvaluator;
  output: CheckOutput;
  trigger: CheckTrigger;
  threshold: CheckThreshold | null;
  route_to_inbox: boolean;
  enabled: boolean;
  created_by: string | null;
  approved_by: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AssuranceCheckCreate {
  name: string;
  description_nl?: string | null;
  scope?: { ai_system_ids?: string[] };
  tier: CheckTier;
  evaluator: CheckEvaluator;
  output?: CheckOutput;
  threshold?: CheckThreshold | null;
  route_to_inbox?: boolean;
}

export interface AssuranceCheckUpdate {
  name?: string;
  description_nl?: string | null;
  scope?: { ai_system_ids?: string[] };
  evaluator?: CheckEvaluator;
  output?: CheckOutput;
  threshold?: CheckThreshold | null;
  route_to_inbox?: boolean;
  enabled?: boolean;
}

export interface CheckBacktestSummary {
  evaluated?: number;
  breaches?: number;
  errors?: number;
  window_days?: number;
  ran_at?: string;
}

export interface AssuranceCheckVersion {
  id: string;
  check_id: string;
  version: number;
  spec_hash: string;
  spec: Record<string, unknown>;
  backtest_summary: CheckBacktestSummary | null;
  created_by: string | null;
  created_at: string | null;
}

export interface AssuranceCheckResult {
  id: string;
  check_id: string;
  check_version: number;
  run_id: string | null;
  window_start: string | null;
  window_end: string | null;
  value: { value?: unknown; hits?: number; total?: number };
  evidence: Record<string, unknown>;
  evaluator_fingerprint: string;
  backtest: boolean;
  tokens_used: number | null;
  evaluated_at: string | null;
}

export interface CheckResultListResponse {
  results: AssuranceCheckResult[];
  total: number;
}

export interface CheckCompileResponse {
  ok: boolean;
  errors: string[];
  sample_value?: unknown;
  context_version?: number;
}

export interface CheckBacktestResponse {
  ok: boolean;
  error?: string;
  evaluated?: number;
  breaches?: number;
  errors?: number;
  window_days?: number;
  samples?: { run_id: string; value: unknown; breached: boolean }[];
}

export interface CheckCompileAssistResponse {
  proposal: {
    tier?: CheckTier;
    reason?: string;
    evaluator?: CheckEvaluator;
    output?: CheckOutput;
    threshold?: CheckThreshold | null;
  } | null;
  error?: string;
  model?: string;
  downgraded?: boolean;
  compile?: CheckCompileResponse | null;
}

// ── Weekly digest ─────────────────────────────────────────────

export interface DigestSystemSection {
  ai_system_id: string;
  name: string;
  health_state: string;
  health_score: number | null;
  runs: number;
  cost_usd: number;
  items_raised: number;
  open_critical: number;
  drift_events: number;
}

export interface DigestContent {
  week_start: string;
  week_end: string;
  totals: {
    systems: number;
    runs: number;
    cost_usd: number;
    items_raised: number;
    items_resolved: number;
    open_critical: number;
    open_total: number;
  };
  raised_by_severity: Record<string, number>;
  open_by_severity: Record<string, number>;
  top_check_breaches: {
    title: string;
    occurrences: number;
    severity: string;
    ai_system_id: string | null;
  }[];
  systems: DigestSystemSection[];
}

export interface AssuranceDigest {
  id: string;
  tenant_id: string;
  week_start: string;
  content: DigestContent;
  generated_at: string | null;
}

export interface DigestListResponse {
  digests: AssuranceDigest[];
  total: number;
}
