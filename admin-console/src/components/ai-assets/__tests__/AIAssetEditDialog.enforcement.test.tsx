/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The Asset Register does not act on traffic in this release: an asset's
 * enforcement mode other than "off" would promise what nothing does. The
 * edit dialog offers no enforcement mode, sends none, and shows a stored
 * "warn" or "enforce" read-only as not available in this release.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';

const gatewayClient = { createAIAsset: vi.fn().mockResolvedValue({}), updateAIAsset: vi.fn().mockResolvedValue({}) };
vi.mock('../../../hooks/useGateway', () => ({ useGateway: () => ({ gatewayClient }) }));
vi.mock('../../common/Toast', () => ({ useToast: () => ({ success: vi.fn(), error: vi.fn() }) }));

import AIAssetEditDialog from '../AIAssetEditDialog';
import type { AIAsset } from '../../../lib/aiAssetTypes';

const ASSET = {
    id: 'a1',
    asset_kind: 'llm_model',
    ref_id: 'gpt',
    display_name: 'GPT',
    lifecycle_stage: 'production',
    enforcement_mode: 'enforce',
    status: 'ok',
    tags: [],
} as unknown as AIAsset;

afterEach(() => {
    cleanup();
    vi.clearAllMocks();
});

describe('AIAssetEditDialog: the enforcement mode', () => {
    it('is not offered, and an update sends none', async () => {
        render(<AIAssetEditDialog mode="edit" initial={ASSET} onClose={vi.fn()} onSaved={vi.fn()} />);
        expect(screen.queryAllByRole('combobox').map((select) => select.getAttribute('value'))).not.toContain('enforce');
        expect(screen.queryByRole('option', { name: /Enforce/ })).toBeNull();
        expect(screen.getByTestId('asset-enforcement-unavailable')).toHaveTextContent('enforce — not available in this release');
        fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));
        await waitFor(() => expect(gatewayClient.updateAIAsset).toHaveBeenCalled());
        expect(gatewayClient.updateAIAsset.mock.calls[0][1]).not.toHaveProperty('enforcement_mode');
    });

    it('a stored "off" shows nothing', () => {
        render(<AIAssetEditDialog mode="edit" initial={{ ...ASSET, enforcement_mode: 'off' }} onClose={vi.fn()} onSaved={vi.fn()} />);
        expect(screen.queryByTestId('asset-enforcement-unavailable')).toBeNull();
    });
});
