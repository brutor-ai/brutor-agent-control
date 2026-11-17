/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A `GET /v1/admin/ai-systems/{id}/composition` payload shaped as the
 * control plane returns it: the Brutor Demo System (loan pre-screening)
 * with a critical shared-service dependency on the fraud screener, an
 * external credit bureau agent, a declared-but-unused dependency, an
 * observed-but-undeclared callee, one declared and one unaccepted caller.
 */

import type { AISystemCompositionView, CompositionConnection, DependencyEdge } from '../../../../lib/aiSystemTypes';

/** A connector line's status as the control plane sends it. */
export function conn(
    status: CompositionConnection['status'],
    reason: string,
    calls = 0,
    errors = 0,
    blocked = 0,
    held = 0,
    held_lapsed = 0
): CompositionConnection {
    return {
        status, reason, calls, errors, blocked, held, held_lapsed,
        last_call_at: calls ? '2026-09-26T08:00:00Z' : null,
    };
}

const IDLE = conn('idle', 'No calls in the last 7 days');

const AUTHORSHIP = {
    declared_by: 'admin',
    declared_at: '2026-09-20T10:00:00Z',
    updated_by: 'admin',
    updated_at: '2026-09-20T10:00:00Z',
};

export const FRAUD_EDGE: DependencyEdge = {
    id: 'dep-fraud',
    ai_system_id: 'sys-demo',
    role: 'shared_service',
    depends_on_system_id: 'sys-fraud',
    depends_on_system_name: 'Fraud Screener',
    agent_card_id: 'card-fraud',
    agent_card_name: 'fraud-screener-card',
    criticality: 'critical',
    fallback: null,
    contributes_to_decision: true,
    separability_rationale: null,
    separability_record_id: null,
    separability_sealed_at: null,
    contract_pin: null,
    supplier_legal_entity: null,
    assumptions: {},
    ...AUTHORSHIP,
};

export const BUREAU_EDGE: DependencyEdge = {
    id: 'dep-bureau',
    ai_system_id: 'sys-demo',
    role: 'external',
    depends_on_system_id: null,
    depends_on_system_name: null,
    agent_card_id: 'card-bureau',
    agent_card_name: 'Credit Bureau Agent',
    criticality: 'degradable',
    fallback: 'Route to manual underwriting',
    contributes_to_decision: true,
    separability_rationale: null,
    separability_record_id: null,
    separability_sealed_at: null,
    contract_pin: null,
    // An external agent names its supplier and carries
    // typed assumptions (strict schema).
    supplier_legal_entity: 'Nordic Credit Bureau AB',
    assumptions: {
        data_classes_shared: ['personal_data', 'financial'],
        expected_availability_pct: 99.5,
        evidence_provided: ['contract', 'signed_agent_card'],
    },
    ...AUTHORSHIP,
};

export const ADDRESS_EDGE: DependencyEdge = {
    id: 'dep-address',
    ai_system_id: 'sys-demo',
    role: 'shared_service',
    depends_on_system_id: 'sys-address',
    depends_on_system_name: 'Address Validator',
    agent_card_id: null,
    agent_card_name: null,
    criticality: 'degradable',
    fallback: 'Accept the applicant-supplied address',
    contributes_to_decision: false,
    separability_rationale: 'Formats addresses; never read by the scoring step.',
    separability_record_id: 'rec-sep-address-0001',
    separability_sealed_at: '2026-09-21T08:30:00Z',
    contract_pin: 'ctr-address-2',
    supplier_legal_entity: null,
    assumptions: {},
    ...AUTHORSHIP,
};

const NO_CALLS = { calls: 0, answered: 0, failed: 0, rejected: 0, timed_out: 0, blocked: 0, last_seen_at: null };

export const COMPOSITION_PAYLOAD: AISystemCompositionView = {
    system: {
        id: 'sys-demo',
        name: 'Brutor Demo System',
        system_kind: 'agent',
        owner: 'Credit Risk',
        lifecycle_stage: 'active',
        autonomy_level: 'approval_required',
        active_contract: { id: 'ctr-demo-3', version: 3 },
        risk_tier_declared: 'high',
        risk_tier_effective: 'high',
        effective_tier_sources: [],
        separability_declared: true,
        // The address edge's sealed claim (this system
        // declares it) and one a caller declares on this system, unsealed.
        separability_declarations: [
            {
                edge_id: 'dep-address',
                ai_system_id: 'sys-demo',
                ai_system_name: 'Brutor Demo System',
                depends_on_system_id: 'sys-address',
                depends_on_system_name: 'Address Validator',
                agent_card_id: null,
                agent_card_name: null,
                side: 'declares',
                separability_rationale: 'Formats addresses; never read by the scoring step.',
                record_id: 'rec-sep-address-0001',
                sealed_at: '2026-09-21T08:30:00Z',
                sealed: true,
            },
            {
                edge_id: 'dep-portal-demo',
                ai_system_id: 'sys-portal',
                ai_system_name: 'Broker Portal Assistant',
                depends_on_system_id: 'sys-demo',
                depends_on_system_name: 'Brutor Demo System',
                agent_card_id: null,
                agent_card_name: null,
                side: 'declared_on',
                separability_rationale: 'Shows the pre-screening status; takes no decision on it.',
                record_id: null,
                sealed_at: null,
                sealed: false,
            },
        ],
        enforce_accepted_callers: true,
    },
    window_hours: 168,
    location: [
        { id: 'grp-tenant', name: 'Nordica', group_type: 'organization' },
        { id: 'grp-lending', name: 'Lending', group_type: 'department' },
        { id: 'sys-demo', name: 'Brutor Demo System', group_type: 'ai_system' },
    ],
    // One member agent with a verified, approved release; one
    // that declares its own release under an observe-only identity.
    agents: [
        {
            id: 'agent-intake',
            name: 'Intake Agent',
            implementation_name: 'loan-intake',
            approved_versions: ['1.4.*'],
            require_release: true,
            release: {
                name: 'loan-intake',
                version: '1.4.2',
                build: 'sha256:ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12',
                trust: 'verified',
                approved: true,
                last_seen_at: '2026-09-27T08:00:00Z',
            },
        },
        {
            id: 'agent-scorer',
            name: 'Scoring Agent',
            implementation_name: null,
            approved_versions: [],
            require_release: false,
            release: {
                name: 'scoring-agent',
                version: '0.9.0',
                build: null,
                trust: 'declared',
                approved: null,
                last_seen_at: '2026-09-27T07:00:00Z',
            },
        },
    ],
    resources: {
        models: [
            { id: 'llm-sonnet', name: 'Claude Sonnet 5', inherited_from: null, connection: conn('ok', '12 calls, 0 errors', 12) },
            {
                id: 'llm-mini',
                name: 'GPT-5 mini',
                inherited_from: { id: 'grp-lending', name: 'Lending' },
                connection: conn('degraded', '3 of 20 calls failed', 20, 3),
            },
        ],
        mcp_servers: [
            {
                id: 'mcp-core',
                name: 'Loan Core',
                inherited_from: null,
                system: false,
                mcp_server_id: 'srv-core',
                connection: conn('down', 'Last 3 calls blocked by policy', 9, 0, 5),
            },
            { id: 'mcp-bureau', name: 'Bureau Lookup', inherited_from: null, system: false, mcp_server_id: 'srv-bureau', connection: IDLE },
            // The platform's own Agent Skills server — gateway plumbing,
            // left out of the Simple graph's MCP lane.
            {
                id: 'mcp-agent-skills',
                name: 'Agent Skills',
                inherited_from: { id: 'grp-tenant', name: 'Nordica' },
                system: true,
                connection: conn('ok', '4 calls, 0 errors', 4),
            },
        ],
        skills: [
            { id: 'skill-affordability', name: 'affordability-check', inherited_from: null, connection: conn('ok', '4 calls, 0 errors', 4) },
        ],
        knowledge_bases: [
            { id: 'kb-policy', name: 'Credit Policy Handbook', inherited_from: { id: 'grp-tenant', name: 'Nordica' }, connection: IDLE },
        ],
    },
    system_status: conn('ok', 'Health: healthy · 57 calls, 4 errors', 57, 4),
    gateway: {
        policies: [
            {
                kind: 'guardrail',
                id: 'gr-pii',
                name: 'PII & Secrets',
                summary: 'Redacts personal data on input and output',
                inherited_from: { id: 'grp-tenant', name: 'Nordica' },
            },
            { kind: 'argument_policy', id: 'ap-amount', name: 'Loan amount ceiling', summary: 'amount ≤ 50 000', inherited_from: null },
            { kind: 'governance', id: null, name: 'Human approval on decline', summary: 'approval_required', inherited_from: null },
        ],
        limits: [
            { kind: 'budget', key: 'monthly_budget_usd', value: 500, summary: '$500 / month', inherited_from: { id: 'grp-lending', name: 'Lending' } },
            { kind: 'run_cap', key: 'max_actions_per_run', value: 40, summary: '40 actions per run', inherited_from: null },
        ],
    },
    human_approval: { autonomy_requires_approval: false, approval_window_seconds: null, gates: [] },
    dependencies: [
        {
            ...FRAUD_EDGE,
            declared: true,
            granted: true,
            health: 'healthy',
            observed: { calls: 12, answered: 11, failed: 1, rejected: 0, timed_out: 0, blocked: 0, last_seen_at: '2026-09-26T08:00:00Z' },
            target_contract: { id: 'ctr-fraud-2', version: 2, pinned: false },
            connection: conn('degraded', '1 of 12 calls failed', 12, 1),
        },
        {
            ...BUREAU_EDGE,
            declared: true,
            granted: true,
            health: 'assumed',
            observed: { calls: 5, answered: 5, failed: 0, rejected: 0, timed_out: 0, blocked: 0, last_seen_at: '2026-09-26T07:00:00Z' },
            target_contract: null,
            connection: conn('ok', '5 calls, 0 errors', 5),
        },
        {
            ...ADDRESS_EDGE,
            declared: true,
            granted: true,
            health: 'idle',
            observed: NO_CALLS,
            target_contract: { id: 'ctr-address-2', version: 2, pinned: true },
            connection: IDLE,
        },
        // An a2a_call grant reaches this card; nobody declared the edge and it
        // has not been called yet.
        {
            id: null,
            ai_system_id: 'sys-demo',
            role: null,
            depends_on_system_id: null,
            depends_on_system_name: null,
            agent_card_id: 'card-weather',
            agent_card_name: 'Weather Agent',
            criticality: null,
            fallback: null,
            contributes_to_decision: null,
            separability_rationale: null,
            separability_record_id: null,
            separability_sealed_at: null,
            contract_pin: null,
            supplier_legal_entity: null,
            assumptions: {},
            declared_by: null,
            declared_at: null,
            updated_by: null,
            updated_at: null,
            declared: false,
            granted: true,
            health: 'unknown',
            observed: NO_CALLS,
            target_contract: null,
            connection: IDLE,
        },
    ],
    observed_undeclared: [
        {
            system_id: 'sys-kyc',
            system_name: 'KYC Checker',
            calls: 3,
            last_seen_at: '2026-09-25T12:00:00Z',
            connection: conn('ok', '3 calls, 0 errors', 3),
        },
    ],
    dependents: [
        { system_id: 'sys-portal', system_name: 'Broker Portal Assistant', declared: true, edge_declared: true, observed_calls: 40, last_seen_at: '2026-09-26T09:00:00Z' },
        { system_id: 'sys-shadow', system_name: 'Shadow Batch Job', declared: false, edge_declared: false, observed_calls: 2, last_seen_at: '2026-09-24T09:00:00Z' },
    ],
    client_labels: [{ label: 'Loan Officer Portal', declared: true, observed_calls: 18 }],
    findings: [
        {
            code: 'composition.undeclared_dependency',
            severity: 'warning',
            message: 'Called KYC Checker 3 times with no declared dependency.',
            subject_id: 'sys-kyc',
        },
        {
            code: 'composition.unaccepted_caller',
            severity: 'warning',
            message: 'Shadow Batch Job called this system but is not an intended client.',
            subject_id: 'sys-shadow',
        },
        {
            code: 'composition.declared_never_observed',
            severity: 'info',
            message: 'Address Validator is declared but was not called in the window.',
            subject_id: 'dep-address',
        },
    ],
};
