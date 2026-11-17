/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A `GET /v1/admin/runs/{id}` payload as the control plane's run-action
 * projection (`app/services/run_actions.py`) shapes it, for the Brutor Demo
 * System of `compositionPayload.ts`. The array is in the GATEWAY's order,
 * so the fraud screener's own steps (5a, 5b) arrive BEFORE the A2A call (5)
 * that entered them — the gateway logs a call when it returns.
 *
 *   1  applications_get        Loan Core       answered   gather
 *   2  bureau_verify_identity  Bureau Lookup   HELD       gather
 *   3  applications_get        Loan Core       answered   gather
 *   4  affordability-check · affordability.py  answered   gather
 *   5  screening.fraud_sanctions → Fraud Screener  answered  assess
 *     5a  gpt-5-mini (the screener's model call)  answered
 *     5b  sanctions_check (the screener's tool)   BLOCKED
 *   6  claude-sonnet-5 (retrieves Credit Policy Handbook)  answered  assess
 *   7  applications_get        Loan Core       ERROR      decide
 *   8  crm_lookup              Shadow CRM      BLOCKED    decide  ← not declared
 *   9  kyc.check → KYC Checker (observed, undeclared node)  answered  decide
 *
 * Every free-text field a metadata-only view must never show carries
 * `SENTINEL`, and so do fields the payload must never have had.
 */

import type { RunAction, RunDetail } from '../../../../lib/aiSystemTypes';

export const SENTINEL = 'SENTINEL-applicant-income-4711';

const GATHER = { step_id: 's-gather', step_name: 'Gather the application facts' };
const ASSESS = { step_id: 's-assess', step_name: 'Assess risk and fraud' };
const DECIDE = { step_id: 's-decide', step_name: 'Decide and record' };

function action(number: string, fields: Partial<RunAction>): RunAction {
    return {
        id: `log-${number}`,
        timestamp: '2026-09-29T08:41:30Z',
        proxy_type: 'mcp',
        success: true,
        status_code: 200,
        duration_ms: 25,
        number,
        entered_by: null,
        nesting: 0,
        outcome: 'answered',
        own: true,
        system_id: 'sys-demo',
        system_name: 'Brutor Demo System',
        kb_ids: [],
        // Content a metadata-only view must never render.
        error_message: `${SENTINEL} (error text)`,
        ...fields,
    };
}

/** Fields the projection never sends; a leaked renderer would show them. */
const LEAKS = {
    request_body_preview: `${SENTINEL} (request body)`,
    response_body_preview: `${SENTINEL} (response body)`,
    arguments: { income: SENTINEL },
    prompt: `${SENTINEL} (prompt)`,
} as unknown as Partial<RunAction>;

export const RUN_ACTIONS: RunAction[] = [
    action('1', { ...GATHER, turn_id: 't1', turn_seq: 1, target_kind: 'mcp', target_id: 'srv-core', target_name: 'Loan Core', capability: 'applications_get', ...LEAKS }),
    action('2', {
        ...GATHER, turn_id: 't1', turn_seq: 1, target_kind: 'mcp', target_id: 'srv-bureau', target_name: 'Bureau Lookup',
        capability: 'bureau_verify_identity', outcome: 'held', proxy_subtype: 'approval_required', success: false, status_code: 403,
    }),
    action('3', { ...GATHER, turn_id: 't1', turn_seq: 1, target_kind: 'mcp', target_id: 'srv-core', target_name: 'Loan Core', capability: 'applications_get' }),
    action('4', {
        ...GATHER, turn_id: 't1', turn_seq: 1, target_kind: 'skill', target_id: 'skill-affordability', target_name: 'affordability-check',
        skill_script: 'affordability.py', capability: 'skills__run_script', proxy_subtype: 'skill_run',
    }),
    // The delegate's steps — before their call in the gateway's order.
    action('5a', {
        proxy_type: 'llm', model: 'gpt-5-mini', target_kind: 'model', target_id: 'llm-mini', target_name: 'GPT-5 mini',
        entered_by: '5', nesting: 1, own: false, system_id: 'sys-fraud', system_name: 'Fraud Screener', inferred_turn: 2, ...LEAKS,
    }),
    action('5b', {
        target_kind: 'mcp', target_id: 'srv-sanctions', target_name: 'Sanctions', capability: 'sanctions_check', outcome: 'blocked',
        entered_by: '5', nesting: 1, own: false, system_id: 'sys-fraud', system_name: 'Fraud Screener', guardrail_blocked: true,
    }),
    action('5', {
        ...ASSESS, turn_id: 't2', turn_seq: 2, proxy_type: 'a2a', proxy_subtype: 'outbound', target_kind: 'a2a', target_id: 'card-fraud',
        target_name: 'fraud-screener-card', capability: 'screening.fraud_sanctions', delegate_system_id: 'sys-fraud',
        delegate_system_name: 'Fraud Screener', duration_ms: 1250,
    }),
    action('6', {
        ...ASSESS, turn_id: 't2', turn_seq: 2, proxy_type: 'llm', model: 'claude-sonnet-5', target_kind: 'model', target_id: 'llm-sonnet',
        target_name: 'Claude Sonnet 5', kb_ids: ['kb-policy'], duration_ms: 2400,
    }),
    action('7', {
        ...DECIDE, turn_id: 't3', turn_seq: 3, target_kind: 'mcp', target_id: 'srv-core', target_name: 'Loan Core', capability: 'applications_get',
        outcome: 'error', success: false, status_code: 502,
    }),
    action('8', {
        ...DECIDE, turn_id: 't3', turn_seq: 3, target_kind: 'mcp', target_id: 'srv-shadow-crm', target_name: 'Shadow CRM', capability: 'crm_lookup',
        outcome: 'blocked', success: false, status_code: 403,
    }),
    action('9', {
        ...DECIDE, turn_id: 't3', turn_seq: 3, proxy_type: 'a2a', target_kind: 'a2a', target_id: 'card-kyc', target_name: 'kyc-card',
        capability: 'kyc.check', delegate_system_id: 'sys-kyc', delegate_system_name: 'KYC Checker',
    }),
];

export const OVERLAY_RUN: RunDetail = {
    id: 'run-overlay-1',
    root_task_id: 'root-overlay-1',
    ai_system_id: 'sys-demo',
    ai_system_name: 'Brutor Demo System',
    via_system_ids: ['sys-fraud'],
    terminal_state: 'completed',
    finalized_by: 'explicit',
    action_count: RUN_ACTIONS.length,
    step_count: 3,
    llm_call_count: 2,
    tool_call_count: 7,
    max_delegation_depth: 1,
    distinct_tools: 5,
    total_tokens: 4200,
    total_cost_usd: 0.021,
    error_count: 1,
    tool_error_count: 1,
    guardrail_block_count: 2,
    policy_denial_count: 0,
    chain_integrity: 'intact',
    reopened_count: 0,
    is_replay: false,
    started_at: '2026-09-29T08:41:30Z',
    ended_at: '2026-09-29T08:41:40Z',
    subject_id: 'APP-1001',
    subject_kind: 'application',
    agent_impl_name: 'loan-intake',
    agent_impl_version: '1.4.2',
    agent_impl_trust: 'verified',
    actions: RUN_ACTIONS,
    skill_call_count: 1,
    a2a_call_count: 2,
    turn_count: 3,
    turn_attribution: 'client_asserted',
    prompt_tokens: 3000,
    completion_tokens: 1200,
    models_used: [],
    cache_hit_count: 0,
    tokens_saved: 0,
    retry_count: 0,
    fallback_count: 0,
    tool_error_types: {},
    guardrail_trip_count: 2,
    approval_request_count: 1,
    input_bytes: 0,
    output_bytes: 0,
    rag_used: true,
    rag_chunks: 3,
    is_successful: true,
    is_baseline_eligible: true,
    is_fragmented: false,
};

/** A long run: `n` top-level model / tool calls in turn order. */
export function crowdedRun(n: number, targetOf: (i: number) => Partial<RunAction>): RunDetail {
    const actions = Array.from({ length: n }, (_, k) =>
        action(String(k + 1), { ...GATHER, turn_seq: k + 1, turn_id: `t${k + 1}`, ...targetOf(k + 1) })
    );
    return { ...OVERLAY_RUN, id: 'run-crowded', actions, action_count: n };
}
