/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of the AI assets page test share: the toast and gateway
 * client doubles (the same objects on every render) and an asset. The page's
 * own cases are Community tests (`__tests__/AIAssetsTab.test.tsx`); the cases
 * that need a real Enterprise item on the page are under
 * `enterprise/src/host/`.
 *
 * A `vi.mock` cannot be shared (it is hoisted in the file it is written in):
 * each test file writes its own calls and takes the modules from here. This
 * module does not import the page, so that a mock factory can await it.
 */

import { vi } from 'vitest';

import type { AIAsset } from '../../lib/aiAssetTypes';

// Singleton toast — same reference every render so the useCallback
// chain in AIAssetsTab.refresh doesn't churn.
export const stableToast = {
    showToast: vi.fn(),
    success: vi.fn(),
    error: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
};

/** `components/common/Toast`. */
export const toastModule = () => ({ useToast: () => stableToast });

/**
 * `components/common/ConfirmationDialog`: every confirmation is accepted (the
 * delete path is not exercised, but the hook must exist).
 */
export const confirmModule = () => ({ useConfirm: () => vi.fn().mockResolvedValue(true) });

export const mockListAIAssets = vi.fn();
export const mockSyncAIAssets = vi.fn();
export const mockCreateAIAsset = vi.fn();
export const mockUpdateAIAsset = vi.fn();
export const mockDeleteAIAsset = vi.fn();
export const mockFetchAssetCoverage = vi.fn();

// Singleton gateway client — stable reference to avoid the refresh
// useCallback recomputing and looping the useEffect.
const stableGatewayClient = {
    listAIAssets: mockListAIAssets,
    syncAIAssets: mockSyncAIAssets,
    createAIAsset: mockCreateAIAsset,
    updateAIAsset: mockUpdateAIAsset,
    deleteAIAsset: mockDeleteAIAsset,
    // Coverage scorecard + shadow-AI list. Best-effort: the component
    // tolerates rejection.
    fetchAssetCoverage: mockFetchAssetCoverage,
    // AssetIcon resolves leading-slash icon paths against the gateway base.
    resolveAssetUrl: (ref: string) =>
        ref.startsWith('/') ? `http://cp${ref}` : ref,
};

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => ({ gatewayClient: stableGatewayClient }) });

/**
 * `hooks/useAuth`: the page reads `hasPermission` to gate the Sync action and
 * is not rendered inside an `AuthProvider` here; every permission is granted,
 * so the full UI, Sync now included, is exercised.
 */
export const useAuthModule = () => ({ useAuth: () => ({ hasPermission: () => true }) });

export function buildAsset(overrides: Partial<AIAsset> = {}): AIAsset {
    return {
        id: 'asset-1',
        tenant_id: 't1',
        asset_kind: 'llm_model',
        ref_id: 'gpt-4o',
        display_name: 'GPT-4o',
        owner: 'Platform Team',
        lifecycle_stage: 'active',
        risk_tier: 'limited',
        intended_use: 'General chat',
        enforcement_mode: 'warn',
        tags: ['prod'],
        status: 'ok',
        source: 'discovered',
        observed: false,
        created_at: '2026-06-01T00:00:00Z',
        updated_at: '2026-06-10T00:00:00Z',
        ...overrides,
    };
}
