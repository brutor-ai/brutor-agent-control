/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * What the two halves of `AIAssetsTab.discovery.test` share: the gateway
 * client and permission doubles, an asset, and the coverage the gateway
 * answers with. The Community half is the page with nothing contributed; the
 * discovery section is the Enterprise extension's, and its cases are under
 * `enterprise/src/host/`. The toast and the confirmation dialog are the page
 * test's (`aiAssetsTab`).
 */

import { vi } from 'vitest';

import type { AIAsset } from '../../lib/aiAssetTypes';
import { buildAsset } from './aiAssetsTab';

export const listAIAssets = vi.fn();
export const syncAIAssets = vi.fn();
export const deleteAIAsset = vi.fn();
export const fetchAssetCoverage = vi.fn();
const stableClient = {
    listAIAssets,
    syncAIAssets,
    deleteAIAsset,
    fetchAssetCoverage,
    resolveAssetUrl: (ref: string) => ref,
};

/** `hooks/useGateway`. */
export const useGatewayModule = () => ({ useGateway: () => ({ gatewayClient: stableClient }) });

/** What the signed-in administrator may do; a case narrows `granted`. */
export const permissions: { granted: (permission: string) => boolean } = { granted: () => true };

/** `hooks/useAuth`. */
export const useAuthModule = () => ({
    useAuth: () => ({ hasPermission: (permission: string) => permissions.granted(permission) }),
});

export const ASSET: AIAsset = buildAsset({ tags: [] });
export const COVERAGE = {
    coverage: { governed: 27, observed: 4, ungoverned: 6, total_seen: 37 },
    discovered_ungoverned: [
        {
            id: 'dasset-1',
            asset_key: 'llm:api.openai.com/v1/chat/completions',
            display_name: 'OpenAI chat',
            asset_type: 'llm_endpoint',
            provider: 'openai',
            computed_confidence: 0.94,
            evidence_count: 99,
            last_seen: '2026-06-29T00:00:00Z',
        },
    ],
};

/** How every case starts: one asset, the coverage above, every permission. For a `beforeEach`. */
export function resetDiscovery(): void {
    vi.clearAllMocks();
    permissions.granted = () => true;
    listAIAssets.mockResolvedValue({ assets: [ASSET], total: 1 });
    fetchAssetCoverage.mockResolvedValue(COVERAGE);
    syncAIAssets.mockResolvedValue({ created: 1, updated: 0, orphaned: 0, total: 1 });
    deleteAIAsset.mockResolvedValue(undefined);
}
