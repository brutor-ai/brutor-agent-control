/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the tests of a slot of the Mission Control page share (the Caching tab
 * and its tile, the Discovery Coverage section; a Community half under
 * `components/monitoring/__tests__/` and an Enterprise half under
 * `enterprise/src/host/` each): the gateway double, the page's main load, and
 * the answers the slot items read.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file mocks `useGateway` and `useToast` itself and takes the
 * modules from here. This module does not import the page, so that a mock
 * factory can await it.
 */

import { vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';

/** Everything else the page calls resolves to null (see MissionControl.test.tsx). */
export const autoStub = (overrides: Record<string, unknown>): Record<string, unknown> =>
    new Proxy(overrides, {
        get(target, prop: string) {
            if (prop in target) return target[prop];
            return vi.fn().mockResolvedValue(null);
        },
        has: () => true,
    });

// The page's main load, answered as in MissionControl.test.tsx so the page leaves its loading state.
export const mainLoad = {
    fetchServers: vi.fn(),
    fetchRealTimeMetrics: vi.fn(),
    fetchPerformanceMetrics: vi.fn(),
    fetchDashboardMetrics: vi.fn(),
    fetchAllUsageAlerts: vi.fn(),
};

/**
 * `hooks/useGateway`. `client` names the gateway-client methods the test file
 * answers; it is called at the first render, so it may name mocks the file
 * declares after its `vi.mock` calls.
 */
export function useGatewayModule(client: () => Record<string, unknown>) {
    let stubClient: Record<string, unknown> | undefined;
    return {
        useGateway: () => {
            stubClient ??= autoStub(client());
            return autoStub({ ...mainLoad, isAuthenticated: true, gatewayClient: stubClient });
        },
    };
}

export const toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };

/** `components/common/Toast`. */
export const toastModule = () => ({ useToast: () => toast });

/** `components/common/ConfirmationDialog`: every confirmation is accepted. */
export const confirmModule = () => ({ useConfirm: () => vi.fn().mockResolvedValue(true) });

/** How every case starts: the page's main load answered, with one server (`srv-alpha`). For a `beforeEach`. */
export function resetMissionControlPage(): void {
    vi.clearAllMocks();
    vi.spyOn(console, 'error').mockImplementation(() => {});
    localStorage.clear();
    mainLoad.fetchServers.mockResolvedValue([]);
    mainLoad.fetchRealTimeMetrics.mockResolvedValue({ real_time_metrics: {} });
    mainLoad.fetchPerformanceMetrics.mockResolvedValue({
        performance_metrics: {
            // The Reliability tiles are shown once there is performance data.
            overview: { total_data_transferred_mb: 1, session_reuse_rate: 50 },
            top_mcp_servers: [{ server_id: 'srv_1', server_name: 'srv-alpha', request_count: 42 }],
        },
    });
    mainLoad.fetchDashboardMetrics.mockResolvedValue({ dashboard: { requests_last_hour: 41, requests_last_day: 9182 } });
    mainLoad.fetchAllUsageAlerts.mockResolvedValue({ alerts: [] });
}

/** Open one of the page's tabs. */
export const tab = (name: string) => fireEvent.click(screen.getByRole('tab', { name }));
/** The labels of the tab strip, in order. */
export const strip = () => screen.getAllByRole('tab').map((t) => t.textContent?.trim());
/** The section headings of the Governance tab, in order. */
export const headings = () => screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent?.trim());
/** The tile (label, value, subtitle) that carries `label`. */
export const tile = (label: string) => screen.getByText(label).parentElement!;

// ── The Caching tab ──────────────────────────────────────────────────────

export const STATS = {
    hit_count: 5,
    miss_count: 15,
    hit_rate: 25,
    tokens_saved: 1200,
    cost_savings_usd: 0.42,
    exact_hits: 3,
    semantic_hits: 1,
    shared_hits: 1,
};
export const VPT = { vpt_score: 1.8, tokens_consumed: 9000, tokens_saved: 1200, cost_savings_usd: 0.42 };

// ── The Governance tab ───────────────────────────────────────────────────

export const DISCOVERY = {
    total: 12,
    governed: 9,
    ungoverned: 3,
    coverage_pct: 75,
    by_type: [
        { asset_type: 'mcp_server', count: 7 },
        { asset_type: 'llm_model', count: 5 },
    ],
    top_ungoverned: [
        { id: 'asset-1', asset_type: 'mcp_server', asset_key: 'https://mcp.acme.test/sse', confidence: 0.87 },
        { id: 'asset-2', asset_type: 'llm_model', asset_key: 'gpt-4o@desktop' },
    ],
};

export const posture = (discovery?: unknown) => ({
    guardrails: { blocked: 4, block_rate: 1.5, policy_denied: 2, pii_detected: 6, total_requests: 260 },
    shadow_ai: { observed_cost: 1.5, observed_requests: 30, unapproved_requests: 8, unapproved_models: [] },
    ...(discovery === undefined ? {} : { discovery }),
});
