/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLM Catalog search — the empty-table trap.
 *
 * The provider and mode filters reset `offset` in their `onChange`, and so
 * must the search box as it is typed in. Otherwise typing a new query while on
 * page 3 sends `offset: 100` alongside the new `q`, and if the new query matches
 * fewer than 100 rows the table comes back empty while the footer reads "page 3
 * of 1". The operator's reasonable conclusion — that the model is not in the
 * catalog — is wrong, and nothing on screen contradicts it.
 *
 * The second half is cost: with `q` directly in the load effect's dependency
 * chain, every character would be a `listLLMCatalog` round trip.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';

const mockListLLMCatalog = vi.fn();
// Stable identity: `refresh` closes over `gatewayClient`, so handing back a
// fresh object each render would re-fire the load effect on every render and
// make the debounce assertion below measure the mock, not the component.
const stubClient = { listLLMCatalog: mockListLLMCatalog };

vi.mock('../../../../hooks/useGateway', () => ({
    useGateway: () => ({ gatewayClient: stubClient }),
}));
vi.mock('../../../common/Toast', () => ({
    useToast: () => ({ success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() }),
}));
vi.mock('../../../common/ConfirmationDialog', () => ({
    useConfirm: () => vi.fn().mockResolvedValue(true),
}));

import LLMCatalogAdminTab from '../LLMCatalogAdminTab';

// These tests wait out a real 300ms debounce. The default 5s budget is ample
// in isolation but not when the whole suite runs in parallel on a loaded
// machine, where the timeout — not the assertion — is what fails.
vi.setConfig({ testTimeout: 20_000 });

function catalog(count: number, total: number) {
    return {
        catalog: Array.from({ length: count }, (_, i) => ({
            id: `cat-${i}`,
            provider_key: 'anthropic',
            model_name: `model-${i}`,
            display_name: `Model ${i}`,
            mode: 'chat',
        })),
        total,
        limit: 100,
        offset: 0,
    };
}

const searchBox = () => screen.getByLabelText('Search catalog');

describe('LLMCatalogAdminTab search', () => {
    beforeEach(() => {
        vi.clearAllMocks();
        vi.spyOn(console, 'error').mockImplementation(() => {});
        mockListLLMCatalog.mockResolvedValue(catalog(100, 260));
    });

    afterEach(() => {
        cleanup();
        vi.restoreAllMocks();
    });

    it('resets to the first page when the query changes', async () => {
        render(<LLMCatalogAdminTab />);
        await waitFor(() => expect(mockListLLMCatalog).toHaveBeenCalled());

        fireEvent.click(await screen.findByRole('button', { name: /Next/i }));
        await waitFor(() =>
            expect(mockListLLMCatalog).toHaveBeenLastCalledWith(
                expect.objectContaining({ offset: 100 }),
            ),
        );

        // Typing from page 2 must not ask for rows 100-199 of a result set
        // that may hold two.
        mockListLLMCatalog.mockResolvedValue(catalog(2, 2));
        fireEvent.change(searchBox(), { target: { value: 'sonnet' } });

        await waitFor(
            () =>
                expect(mockListLLMCatalog).toHaveBeenLastCalledWith(
                    expect.objectContaining({ q: 'sonnet', offset: 0 }),
                ),
            { timeout: 2000 },
        );
    });

    it('debounces the query rather than searching per keystroke', async () => {
        render(<LLMCatalogAdminTab />);
        await waitFor(() => expect(mockListLLMCatalog).toHaveBeenCalled());
        const before = mockListLLMCatalog.mock.calls.length;

        for (const v of ['s', 'so', 'son', 'sonn']) {
            fireEvent.change(searchBox(), { target: { value: v } });
        }

        await waitFor(
            () =>
                expect(mockListLLMCatalog).toHaveBeenLastCalledWith(
                    expect.objectContaining({ q: 'sonn' }),
                ),
            { timeout: 2000 },
        );
        expect(mockListLLMCatalog.mock.calls.length - before).toBe(1);
    });
});
