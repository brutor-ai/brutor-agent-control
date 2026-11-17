/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Fixtures for the assurance report's "Composition and
 * dependencies" section (spec 1.6), shaped per
 * `ai_system_composite_compliance.composition_report_section`: the Brutor
 * Demo System with a tested shared service (pinned contract), an external
 * credit bureau whose supplier agreement is on file, a separable shared
 * service of another legal entity whose agreement is MISSING, one accepted
 * and one refused caller, and one open composition finding.
 */

import type { CompositionReportSection } from '../../../../lib/aiSystemTypes';

export const COMPOSITION_SECTION: CompositionReportSection = {
    title: 'Composition and dependencies',
    window_hours: 168,
    role: 'root_and_service',
    risk_tier_declared: 'limited',
    risk_tier_effective: 'high',
    effective_tier_sources: [{ system_id: 'sys-lending', system_name: 'Lending Decision Engine', tier: 'high' }],
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
            sealed_at: '2026-09-21T08:30:00+00:00',
            sealed: true,
        },
    ],
    enforce_accepted_callers: true,
    edges: [
        {
            edge_id: 'dep-fraud',
            role: 'shared_service',
            depends_on_system_id: 'sys-fraud',
            depends_on_system_name: 'Fraud Screener',
            agent_card_id: 'card-fraud',
            agent_card_name: 'fraud-screener-card',
            criticality: 'critical',
            fallback: null,
            contributes_to_decision: true,
            separability: null,
            contract: { id: 'ctr-fraud-2', version: 2, hash: 'abcdef0123456789', pinned: true },
            assurance_basis: 'tested',
            assumptions: null,
            supplier_legal_entity: null,
            supplier_information: { required: false, on_file: 0, unexpired: 0, evidence: [] },
            status: 'degraded',
            failing_signal: 'reliability',
            caps_root: true,
            boundary: null,
        },
        {
            edge_id: 'dep-bureau',
            role: 'external',
            depends_on_system_id: null,
            depends_on_system_name: null,
            agent_card_id: 'card-bureau',
            agent_card_name: 'Credit Bureau Agent',
            criticality: 'degradable',
            fallback: 'Route to manual underwriting',
            contributes_to_decision: true,
            separability: null,
            contract: null,
            assurance_basis: 'assumed',
            assumptions: { expected_availability_pct: 99.5 },
            supplier_legal_entity: 'Nordic Credit Bureau AB',
            supplier_information: {
                required: true,
                on_file: 1,
                unexpired: 1,
                evidence: [{ id: 'ev-msa', title: 'Bureau MSA 2026', sha256: 'f'.repeat(64), valid_until: null, expired: false }],
            },
            status: 'assumed',
            failing_signal: null,
            caps_root: false,
            boundary: {
                calls: 15,
                errors: 2,
                error_rate: 0.133,
                p95_latency_ms: 420,
                last_seen_at: null,
                task_states: { completed: 12, failed: 2, input_required: 1 },
            },
        },
        {
            edge_id: 'dep-address',
            role: 'shared_service',
            depends_on_system_id: 'sys-address',
            depends_on_system_name: 'Address Validator',
            agent_card_id: null,
            agent_card_name: null,
            criticality: 'degradable',
            fallback: 'Accept the applicant-supplied address',
            contributes_to_decision: false,
            separability: {
                rationale: 'Formats addresses; never read by the scoring step.',
                record_id: 'rec-sep-address-0001',
                sealed_at: '2026-09-21T08:30:00+00:00',
                sealed: true,
            },
            contract: { id: 'ctr-address-3', version: 3, hash: null, pinned: false },
            assurance_basis: 'tested',
            assumptions: null,
            supplier_legal_entity: 'Group Address Services Ltd',
            supplier_information: { required: true, on_file: 0, unexpired: 0, evidence: [] },
            status: 'healthy',
            failing_signal: null,
            caps_root: false,
            boundary: null,
        },
    ],
    callers: [
        { system_id: 'sys-portal', system_name: 'Broker Portal Assistant', accepted: true, observed_calls: 40 },
        { system_id: 'sys-shadow', system_name: 'Shadow Batch Job', accepted: false, observed_calls: 2 },
    ],
    findings: [
        {
            code: 'composition.unaccepted_caller',
            severity: 'warning',
            message: 'Enforced: Shadow Batch Job is not an accepted caller; 2 calls refused.',
            subject_id: 'sys-shadow',
        },
    ],
};
