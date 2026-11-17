/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * AI Asset Factsheet — AIAssetFactsheet test.
 *
 * Scope:
 *   • Usage numbers render from a populated factsheet
 *   • A compliance-framework badge renders
 *   • An access team chip renders
 *   • Changing the period re-calls getFactsheet
 *   • "Capture snapshot" calls captureFactsheetSnapshot then re-lists
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import AIAssetFactsheet from '../components/ai-assets/AIAssetFactsheet';
import type { Factsheet, FactsheetSnapshot } from '../lib/factsheetTypes';

// Singleton toast — stable reference so useCallback chains don't churn.
const _stableToast = {
    showToast: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
};
vi.mock('../components/common/Toast', () => ({
    useToast: () => _stableToast,
}));

const mockGetFactsheet = vi.fn();
const mockExportFactsheetCsv = vi.fn();
const mockCaptureFactsheetSnapshot = vi.fn();
const mockListFactsheetSnapshots = vi.fn();
const mockGetFactsheetSnapshot = vi.fn();

const _stableGatewayClient = {
    getFactsheet: mockGetFactsheet,
    exportFactsheetCsv: mockExportFactsheetCsv,
    captureFactsheetSnapshot: mockCaptureFactsheetSnapshot,
    listFactsheetSnapshots: mockListFactsheetSnapshots,
    getFactsheetSnapshot: mockGetFactsheetSnapshot,
};
vi.mock('../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: _stableGatewayClient }),
}));

function buildFactsheet(overrides: Partial<Factsheet> = {}): Factsheet {
    return {
        asset: {
            id: 'asset-1',
            asset_kind: 'llm_model',
            ref_id: 'gpt-4o',
            display_name: 'GPT-4o',
            owner: 'Platform Team',
            lifecycle_stage: 'active',
            risk_tier: 'limited',
            enforcement_mode: 'warn',
            status: 'ok',
            tags: ['prod'],
        },
        config: { provider: 'openai', model_name: 'gpt-4o', enabled: true },
        usage: {
            requests: 1234,
            prompt_tokens: 5000,
            completion_tokens: 2500,
            total_tokens: 7500,
            estimated_cost_usd: 12.34,
            success_count: 1200,
            error_count: 34,
            error_rate: 0.0276,
            avg_duration_ms: 845,
            first_seen: '2026-06-01T00:00:00Z',
            last_seen: '2026-06-10T00:00:00Z',
            observed_requests: 0,
            observed_cost_usd: 0,
        },
        guardrails: {
            guardrail_blocked: 7,
            pii_detected: 3,
            argument_policy_denied: 1,
        },
        compliance: { frameworks: { 'soc2.cc7.2': 42 } },
        residency: {
            regions_routed_to: ['eu-west-1'],
            residency_class_counts: { eu_only: 10 },
        },
        access: {
            resource_groups: [{ id: 'rg-1', name: 'Engineering' }],
        },
        meta: {
            generated_at: '2026-06-10T12:00:00Z',
            period_hours: 720,
            window_start: '2026-05-11T12:00:00Z',
            window_end: '2026-06-10T12:00:00Z',
        },
        ...overrides,
    };
}

function buildSnapshot(
    overrides: Partial<FactsheetSnapshot> = {},
): FactsheetSnapshot {
    return {
        id: 'snap-1',
        tenant_id: 't1',
        asset_id: 'asset-1',
        period_hours: 720,
        snapshot_json: buildFactsheet(),
        trigger: 'manual',
        created_at: '2026-06-09T09:00:00Z',
        ...overrides,
    };
}

describe('AIAssetFactsheet', () => {
    beforeEach(() => {
        mockGetFactsheet.mockReset();
        mockExportFactsheetCsv.mockReset();
        mockCaptureFactsheetSnapshot.mockReset();
        mockListFactsheetSnapshots.mockReset();
        mockGetFactsheetSnapshot.mockReset();
        _stableToast.success.mockReset();
        _stableToast.error.mockReset();

        mockGetFactsheet.mockResolvedValue(buildFactsheet());
        mockListFactsheetSnapshots.mockResolvedValue({
            snapshots: [buildSnapshot()],
            total: 1,
        });
        mockCaptureFactsheetSnapshot.mockResolvedValue(buildSnapshot());

        // Stub the download path so JSON/CSV export doesn't throw in happy-dom.
        if (!('createObjectURL' in URL)) {
            (URL as unknown as { createObjectURL: () => string }).createObjectURL =
                vi.fn(() => 'blob:mock');
        }
        URL.createObjectURL = vi.fn(() => 'blob:mock');
        URL.revokeObjectURL = vi.fn();
    });

    function renderTab() {
        return render(
            <AIAssetFactsheet
                assetId="asset-1"
                assetName="GPT-4o"
                onBack={vi.fn()}
            />,
        );
    }

    it('renders usage numbers, a compliance badge and an access chip', async () => {
        renderTab();
        await waitFor(() => {
            expect(mockGetFactsheet).toHaveBeenCalled();
        });

        // Usage: requests + total tokens (locale-formatted).
        await waitFor(() => {
            expect(screen.getByText('1,234')).toBeInTheDocument();
        });
        expect(screen.getByText('7,500')).toBeInTheDocument();

        // Compliance framework badge.
        expect(screen.getByText('soc2.cc7.2')).toBeInTheDocument();

        // Access team chip.
        expect(screen.getByText('Engineering')).toBeInTheDocument();
    });

    it('re-calls getFactsheet when the period changes', async () => {
        renderTab();
        await waitFor(() => {
            expect(mockGetFactsheet).toHaveBeenCalledTimes(1);
        });
        expect(mockGetFactsheet).toHaveBeenCalledWith('asset-1', {
            period_hours: 720,
        });

        fireEvent.change(screen.getByLabelText('Factsheet period'), {
            target: { value: '24' },
        });

        await waitFor(() => {
            expect(mockGetFactsheet).toHaveBeenCalledWith('asset-1', {
                period_hours: 24,
            });
        });
    });

    it('captures a snapshot then re-lists snapshots', async () => {
        renderTab();
        await waitFor(() => {
            expect(mockListFactsheetSnapshots).toHaveBeenCalledTimes(1);
        });

        fireEvent.click(
            screen.getByRole('button', { name: /Capture snapshot/i }),
        );

        await waitFor(() => {
            expect(mockCaptureFactsheetSnapshot).toHaveBeenCalledWith(
                'asset-1',
                720,
            );
        });
        // Capture triggers a snapshot re-list.
        await waitFor(() => {
            expect(mockListFactsheetSnapshots).toHaveBeenCalledTimes(2);
        });
        expect(_stableToast.success).toHaveBeenCalledWith('Snapshot captured');
    });
});
