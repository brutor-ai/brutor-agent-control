/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useLLMSearch — browsing the GLOBAL model catalog.
 *
 * The catalog browser is a modal an operator drives with a debounced search
 * box, so the states that matter are the transitional ones: `loading` must be
 * true for the whole request and false again on *both* outcomes (a spinner
 * stuck on after a 500 is a modal the operator has to close and reopen), and a
 * failed search must surface a message while the next successful search clears
 * it again.
 *
 * `search` also has a dual contract — it updates hook state AND returns the
 * response — because the import flow needs the rows immediately, before a
 * re-render lands. Both halves are asserted.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

import type { LLMCatalogListResponse } from '../../lib/llmTypes';

const mocks = vi.hoisted(() => ({
    listLLMCatalog: vi.fn(),
}));

vi.mock('../useAuth', () => ({
    useAuth: () => ({ gatewayClient: { listLLMCatalog: mocks.listLLMCatalog } }),
}));

import { useLLMSearch } from '../useLLMSearch';

function catalogResponse(names: string[]): LLMCatalogListResponse {
    return {
        catalog: names.map((name, i) => ({ id: `c-${i}`, name })),
        total: names.length,
    } as unknown as LLMCatalogListResponse;
}

describe('useLLMSearch', () => {
    beforeEach(() => {
        vi.clearAllMocks();
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('starts idle with nothing loaded', () => {
        const { result } = renderHook(() => useLLMSearch());

        expect(result.current.loading).toBe(false);
        expect(result.current.error).toBeNull();
        expect(result.current.searchResults).toBeNull();
    });

    it('does not query the catalog until asked', () => {
        renderHook(() => useLLMSearch());

        expect(mocks.listLLMCatalog).not.toHaveBeenCalled();
    });

    it('passes the filters straight through to the catalog endpoint', async () => {
        mocks.listLLMCatalog.mockResolvedValue(catalogResponse([]));
        const { result } = renderHook(() => useLLMSearch());

        await act(async () => {
            await result.current.search({ provider_key: 'anthropic', mode: 'chat', q: 'opus' });
        });

        expect(mocks.listLLMCatalog).toHaveBeenCalledWith({
            provider_key: 'anthropic',
            mode: 'chat',
            q: 'opus',
        });
    });

    it('stores the results and also returns them to the caller', async () => {
        const response = catalogResponse(['claude-opus-5', 'claude-sonnet-5']);
        mocks.listLLMCatalog.mockResolvedValue(response);
        const { result } = renderHook(() => useLLMSearch());

        let returned: LLMCatalogListResponse | undefined;
        await act(async () => {
            returned = await result.current.search({ q: 'claude' });
        });

        // The import flow reads the return value; the list reads the state.
        expect(returned).toBe(response);
        expect(result.current.searchResults).toBe(response);
        expect(result.current.error).toBeNull();
    });

    it('raises loading for the duration of the request and lowers it after', async () => {
        let release!: (value: LLMCatalogListResponse) => void;
        mocks.listLLMCatalog.mockReturnValue(
            new Promise<LLMCatalogListResponse>((resolve) => {
                release = resolve;
            })
        );
        const { result } = renderHook(() => useLLMSearch());

        act(() => {
            void result.current.search({ q: 'claude' });
        });
        await waitFor(() => expect(result.current.loading).toBe(true));

        await act(async () => {
            release(catalogResponse(['claude-opus-5']));
            await Promise.resolve();
        });

        await waitFor(() => expect(result.current.loading).toBe(false));
    });

    it('surfaces the failure message, rethrows, and lowers loading', async () => {
        mocks.listLLMCatalog.mockRejectedValue(new Error('catalog unavailable'));
        const { result } = renderHook(() => useLLMSearch());

        await act(async () => {
            await expect(result.current.search({ q: 'claude' })).rejects.toThrow(
                'catalog unavailable'
            );
        });

        expect(result.current.error).toBe('catalog unavailable');
        // The spinner must not survive the failure.
        expect(result.current.loading).toBe(false);
    });

    it('uses a generic message when the rejection is not an Error', async () => {
        mocks.listLLMCatalog.mockRejectedValue('gateway said no');
        const { result } = renderHook(() => useLLMSearch());

        await act(async () => {
            await expect(result.current.search({})).rejects.toBe('gateway said no');
        });

        expect(result.current.error).toBe('Failed to search the model catalog');
    });

    it('clears a stale error when the next search succeeds', async () => {
        mocks.listLLMCatalog.mockRejectedValueOnce(new Error('catalog unavailable'));
        const { result } = renderHook(() => useLLMSearch());

        await act(async () => {
            await expect(result.current.search({})).rejects.toThrow();
        });
        expect(result.current.error).toBe('catalog unavailable');

        mocks.listLLMCatalog.mockResolvedValueOnce(catalogResponse(['gpt-5']));
        await act(async () => {
            await result.current.search({});
        });

        expect(result.current.error).toBeNull();
        expect(result.current.searchResults?.total).toBe(1);
    });

    it('keeps the previous results when a follow-up search fails', async () => {
        const first = catalogResponse(['claude-opus-5']);
        mocks.listLLMCatalog.mockResolvedValueOnce(first);
        const { result } = renderHook(() => useLLMSearch());

        await act(async () => {
            await result.current.search({ q: 'claude' });
        });

        mocks.listLLMCatalog.mockRejectedValueOnce(new Error('catalog unavailable'));
        await act(async () => {
            await expect(result.current.search({ q: 'gpt' })).rejects.toThrow();
        });

        // Blanking the table on a transient failure loses the operator's place.
        expect(result.current.searchResults).toBe(first);
    });

    it('clearResults drops both the rows and the error', async () => {
        mocks.listLLMCatalog.mockRejectedValueOnce(new Error('catalog unavailable'));
        const { result } = renderHook(() => useLLMSearch());

        await act(async () => {
            await expect(result.current.search({})).rejects.toThrow();
        });

        act(() => result.current.clearResults());

        expect(result.current.searchResults).toBeNull();
        expect(result.current.error).toBeNull();
    });
});
