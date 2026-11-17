/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Assurance Inbox — wire types for
 * /v1/admin/assurance-inbox/*.
 */

import { agentImplementationLink } from './agentRelease';

export type InboxState = 'open' | 'acknowledged' | 'resolved';
export type InboxSeverity = 'info' | 'warning' | 'critical';
export type InboxDisposition =
  | 'true_positive'
  | 'false_positive'
  | 'expected'
  | 'fixed';
export type InboxCategory =
  | 'guardrail_block'
  | 'policy_denial'
  | 'contract_violation'
  | 'drift'
  | 'health_transition'
  | 'liveness'
  | 'run_failure'
  | 'budget_stop'
  | 'approval_timeout'
  | 'check_breach'
  | 'notification_failure'
  | 'check_stalled'
  | 'scope_violation'
  // The evidence programme's own findings.
  | 'evidence_unwitnessed'
  | 'capture_gap'
  | 'judge_uncalibrated'
  | 'calibration_stalled'
  | 'incident_deadline'
  // Composition checks (declarations vs grants vs traffic).
  | 'composition'
  // Agent implementation checks (unapproved release, name
  // mismatch, release missing, undeclared change).
  | 'agent';

export const INBOX_CATEGORIES: InboxCategory[] = [
  'guardrail_block',
  'policy_denial',
  'contract_violation',
  'drift',
  'health_transition',
  'liveness',
  'run_failure',
  'budget_stop',
  'approval_timeout',
  'check_breach',
  'notification_failure',
  'check_stalled',
  'scope_violation',
  'evidence_unwitnessed',
  'capture_gap',
  'judge_uncalibrated',
  'calibration_stalled',
  'incident_deadline',
  'composition',
  'agent',
];

/** The words for each category — the inbox chips, its filter and the
 *  notification subscription picker. Typed on the union, so a category the
 *  UI does not name fails the build instead of rendering its slug. */
export const INBOX_CATEGORY_LABELS: Record<InboxCategory, string> = {
  guardrail_block: 'Guardrail',
  policy_denial: 'Policy denial',
  contract_violation: 'Contract',
  drift: 'Drift',
  health_transition: 'Health',
  liveness: 'Liveness',
  run_failure: 'Run failure',
  budget_stop: 'Budget stop',
  approval_timeout: 'Approval timeout',
  check_breach: 'Check breach',
  notification_failure: 'Notification failure',
  check_stalled: 'Check stalled',
  scope_violation: 'Data scope violation',
  // Evidence
  evidence_unwitnessed: 'Evidence unwitnessed',
  capture_gap: 'Capture gap',
  judge_uncalibrated: 'Judge uncalibrated',
  calibration_stalled: 'Calibration stalled',
  incident_deadline: 'Incident deadline',
  composition: 'Composition',
  agent: 'Agent',
};

export function inboxCategoryLabel(category: string): string {
  return (INBOX_CATEGORY_LABELS as Record<string, string>)[category] ?? category;
}

/** An agent finding is worked on the agent identity's
 *  Implementation section (approve the version, fix the declared name);
 *  the system it was raised on is one click further. */
export function agentInboxLinks(
  category: string,
  evidence: Record<string, unknown>,
  aiSystemId: string | null,
): Array<{ label: string; tab: string }> {
  if (category !== 'agent') return [];
  const links: Array<{ label: string; tab: string }> = [];
  const agentId = typeof evidence.agent_id === 'string' ? evidence.agent_id : null;
  if (agentId) {
    links.push({ label: 'View agent implementation', tab: agentImplementationLink(agentId) });
  }
  if (aiSystemId) {
    links.push({ label: 'View composition', tab: `ai-systems:${aiSystemId}:composition` });
  }
  return links;
}

export const INBOX_DISPOSITIONS: InboxDisposition[] = [
  'true_positive',
  'false_positive',
  'expected',
  'fixed',
];

export interface AssuranceInboxItem {
  id: string;
  tenant_id: string;
  ai_system_id: string | null;
  category: InboxCategory;
  severity: InboxSeverity;
  fingerprint: string;
  title: string;
  /** Pointers into native views (run_id, drift_event_id, …) — never content. */
  evidence: Record<string, unknown>;
  occurrence_count: number;
  first_seen_at: string | null;
  last_seen_at: string | null;
  state: InboxState;
  acknowledged_at: string | null;
  acknowledged_by: string | null;
  assignee_admin: string | null;
  disposition: InboxDisposition | null;
  resolution_note: string | null;
  resolved_at: string | null;
  resolved_by: string | null;
}

export interface AssuranceInboxEvent {
  id: string;
  item_id: string;
  at: string | null;
  admin: string | null;
  event:
    | 'created'
    | 'reoccurred'
    | 'acknowledged'
    | 'assigned'
    | 'resolved'
    | 'reopened';
  detail: Record<string, unknown> | null;
}

export interface AssuranceInboxItemDetail extends AssuranceInboxItem {
  events: AssuranceInboxEvent[];
}

export interface AssuranceInboxListResponse {
  items: AssuranceInboxItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface AssuranceInboxCounts {
  open: number;
  acknowledged: number;
  resolved: number;
  /** Unresolved (open + acknowledged) by severity — the sidebar badges. */
  open_critical: number;
  open_warning?: number;
  open_info?: number;
}
