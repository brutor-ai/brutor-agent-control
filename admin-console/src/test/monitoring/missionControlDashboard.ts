/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of the Mission Control card dashboard test share: the
 * gateway doubles, the card fixtures and the starting state of every case.
 * The page's own cases are Community tests
 * (`components/monitoring/__tests__/MissionControlDashboard.test.tsx`); the
 * cases that need a real Enterprise item on the page are under
 * `enterprise/src/host/`.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file mocks `useGateway` itself and takes the module from here.
 * This module does not import the dashboard, so that the mock factory can
 * await it.
 */

import { expect, vi } from 'vitest';
import { waitFor } from '@testing-library/react';

import type { MissionControlCard, MissionControlCardsResponse } from '../../lib/missionControlCardTypes';

export const mockFetchCards = vi.fn();
export const mockFetchExplainers = vi.fn();
export const mockFetchSystemAlerts = vi.fn();
export const mockFetchTenantDriftFeed = vi.fn();
export const mockFetchAllUsageAlerts = vi.fn();
export const mockAcknowledgeUsageAlert = vi.fn();
export const mockAcknowledgeSystemAlert = vi.fn();
export const mockResolveSystemAlert = vi.fn();
export const mockRebuildTenantKbIndex = vi.fn();
export const mockInboxCounts = vi.fn().mockResolvedValue({ open: 0, acknowledged: 0, resolved: 0, open_critical: 0, open_warning: 0 });

export const useGatewayModule = () => ({
    useGateway: () => ({
        fetchMissionControlCards: mockFetchCards,
        fetchMissionControlExplainers: mockFetchExplainers,
        fetchSystemAlerts: mockFetchSystemAlerts,
        fetchTenantDriftFeed: mockFetchTenantDriftFeed,
        fetchAllUsageAlerts: mockFetchAllUsageAlerts,
        acknowledgeUsageAlert: mockAcknowledgeUsageAlert,
        acknowledgeSystemAlert: mockAcknowledgeSystemAlert,
        resolveSystemAlert: mockResolveSystemAlert,
        fetchAssuranceInboxCounts: mockInboxCounts,
        gatewayClient: { rebuildTenantKbIndex: mockRebuildTenantKbIndex },
    }),
});

export function card(overrides: Partial<MissionControlCard> = {}): MissionControlCard {
    return {
        id: 'sys-copilot',
        name: 'Support Copilot',
        group_type: 'ai_system',
        system_kind: 'agent',
        lifecycle_stage: 'active',
        eu_ai_act_risk_tier: null,
        is_group: false,
        systems: 0,
        parent_group_id: null,
        health_state: 'healthy',
        verdict_status: 'assured',
        t_class: 'T(n)',
        t_class_contains: false,
        k_median: 1,
        a_max: 0,
        spend: 12.5,
        runs: 340,
        completed_runs: 330,
        cost_per_task: 0.037,
        cost_per_task_blended: false,
        cpt_bound: null,
        cpt_breached: false,
        trend: [1, 2, 3],
        tokens: 125000,
        data_bytes: 2_400_000,
        trend_tokens: [40000, 40000, 45000],
        trend_bytes: [800000, 800000, 800000],
        live: true,
        findings: 0,
        governance_gaps: 0,
        pseudo: null,
        note: null,
        ...overrides,
    };
}

export const ORG_CARD = card({
    id: 'grp-org',
    name: 'Acme AI',
    group_type: 'custom',
    system_kind: null,
    is_group: true,
    systems: 4,
    health_state: 'drifting',
    verdict_status: 'violations',
    t_class: 'T(n·k)',
    t_class_contains: true,
    cost_per_task_blended: true,
    findings: 2,
});

export const PSEUDO_UNATTRIBUTED = card({
    id: 'pseudo-unattributed',
    name: 'Unattributed runs',
    group_type: 'pseudo',
    pseudo: 'unattributed',
    health_state: null,
    verdict_status: null,
    t_class: null,
    note: 'Governed traffic that matched no AI System.',
});

// A Resource Group that only ever carried imported usage: spend and
// tokens, no runs, and nothing was enforced on any of it.
export const IMPORTED_GROUP = card({
    id: 'grp-imported',
    name: 'Anthropic (imported usage)',
    group_type: 'custom',
    is_group: true,
    systems: 0,
    health_state: null,
    verdict_status: null,
    t_class: null,
    spend: 114.45,
    observed_spend: 114.45,
    runs: 0,
    tokens: 13_600_000,
});

export function response(
    cards: MissionControlCard[],
    overrides: Partial<MissionControlCardsResponse> = {},
): MissionControlCardsResponse {
    return {
        cards,
        total: cards.length,
        window: '30d',
        parent_id: null,
        path: [],
        judgments_pinned_note: 'health/verdict/t_class are pinned…',
        ...overrides,
    };
}

export const EXPLAINERS = {
    explainers: {
        t_class: {
            title: 'Big-T workload class',
            body: 'How this system\u2019s cost grows, measured from its run ledger. Open the Workload classes guide for the full ladder.',
        },
        't_class::T(n)': {
            title: 'T(n) \u2014 Linear',
            body: 'About one model call per finished task. The healthy default.',
            sections: [{ body: 'Measured from this system\u2019s run ledger \u2014 see the Workload classes guide.' }],
        },
        't_class::T(n\u00b7k)': {
            title: 'T(n\u00b7k) \u2014 Multiplicative',
            body: 'Several model calls per task \u2014 k is the median, shown on the card.',
            sections: [{ body: 'Measured from this system\u2019s run ledger \u2014 see the Workload classes guide.' }],
        },
        cost_per_task: { title: 'Cost per completed task', body: 'Spend over finished tasks.' },
        worst_of: { title: 'Worst-of rollups', body: 'Never averages.' },
        findings: { title: 'Open findings', body: 'Unresolved problems on record.' },
    },
    guide: {
        title: 'Workload classes \u2014 the T(n\u00b7k\u00b7a) guide',
        sections: [
            { body: 'Big-T Notation, the first published project of the Tokenomics Foundation.' },
            {
                title: 'The complexity ladder',
                points: [
                    'T(1) \u2014 Constant.',
                    'T(log n) \u2014 Sublinear.',
                    'T(n) \u2014 Linear.',
                    'T(n\u00b7k) \u2014 Multiplicative.',
                    'T(n\u00b7k\u00b7a) \u2014 Agent-multiplicative.',
                    'T(\u221e) \u2014 Unbounded.',
                ],
            },
        ],
    },
};

/** Resolves once the answered cards (or the empty state) are in the DOM; waiting on the mock alone races the commit. */
export async function dashboardLoaded(): Promise<void> {
    await waitFor(() =>
        expect(
            document.querySelector('[data-testid^="mc-card-"], [data-testid="mc-empty"]'),
        ).not.toBeNull(),
    );
}

/** How every case starts: the four cards, the explainers, and no alert anywhere. For a `beforeEach`. */
export function resetDashboard(): void {
    vi.clearAllMocks();
    mockInboxCounts.mockResolvedValue({ open: 0, acknowledged: 0, resolved: 0, open_critical: 0, open_warning: 0 });
    mockFetchCards.mockResolvedValue(response([ORG_CARD, card(), PSEUDO_UNATTRIBUTED, IMPORTED_GROUP]));
    mockFetchExplainers.mockResolvedValue(EXPLAINERS);
    mockFetchSystemAlerts.mockResolvedValue({ alerts: [], summary: {}, timestamp: '', tenant_scope: 'self' });
    mockFetchTenantDriftFeed.mockResolvedValue({ events: [], total: 0, open_count: 0 });
    mockFetchAllUsageAlerts.mockResolvedValue({ alerts: [] });
}
