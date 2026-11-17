/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * useLlmProviders
 *
 * The cache is the whole point of this hook. `LLModelForm` re-renders on every
 * keystroke and passes a fresh `opts` object literal each time; a naive
 * dependency on that object identity would fire a provider request per
 * character. So the tests below drive the two ways that can regress:
 *
 *   - a re-render with an equivalent-but-new `opts` object must NOT re-fetch;
 *   - a genuinely different filter must, because the cache is keyed on the
 *     serialised options, not on the client alone.
 *
 * `refresh()` has to bypass the TTL — it is what runs after the operator edits
 * a provider, and a stale-but-fresh-enough cache entry there would show them
 * their own edit missing. And a failed fetch must leave the previously loaded
 * list on screen rather than blanking the dropdown mid-edit.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, waitFor } from '@testing-library/react';

import type { GatewayApiClient } from '../../lib/gatewayApiClient';
import type { LlmProviderResponse } from '../../lib/llmProviderTypes';
import { useLlmProviders } from '../useLlmProviders';

function provider(key: string): LlmProviderResponse {
    return { id: `p-${key}`, key, display_name: key } as unknown as LlmProviderResponse;
}

/**
 * A distinct client object per test — the module-level cache is a WeakMap
 * keyed on the client, so a fresh client is a fresh cache.
 */
function makeClient(listProviders: ReturnType<typeof vi.fn>): GatewayApiClient {
    return { listProviders } as unknown as GatewayApiClient;
}

describe('useLlmProviders', () => {
    let listProviders: ReturnType<typeof vi.fn>;
    let client: GatewayApiClient;

    beforeEach(() => {
        listProviders = vi.fn().mockResolvedValue({
            providers: [provider('anthropic'), provider('openai')],
            total: 2,
        });
        client = makeClient(listProviders);
    });

    afterEach(() => {
        vi.restoreAllMocks();
    });

    it('fetches on mount and exposes the providers', async () => {
        const { result } = renderHook(() => useLlmProviders(client));

        await waitFor(() => expect(result.current.providers).toHaveLength(2));
        expect(result.current.providers.map((p) => p.key)).toEqual(['anthropic', 'openai']);
        expect(result.current.error).toBeNull();
        expect(result.current.loading).toBe(false);
    });

    it('reports loading while the request is in flight', async () => {
        let release!: (value: { providers: LlmProviderResponse[]; total: number }) => void;
        listProviders.mockReturnValue(
            new Promise((resolve) => {
                release = resolve;
            })
        );

        const { result } = renderHook(() => useLlmProviders(client));

        await waitFor(() => expect(result.current.loading).toBe(true));

        await act(async () => {
            release({ providers: [provider('anthropic')], total: 1 });
            await Promise.resolve();
        });

        await waitFor(() => expect(result.current.loading).toBe(false));
        expect(result.current.providers).toHaveLength(1);
    });

    it('does nothing at all with a null client', async () => {
        const { result } = renderHook(() => useLlmProviders(null));

        expect(result.current.providers).toEqual([]);
        expect(result.current.loading).toBe(false);
        // No client means no tenant context — a request here would 401.
        await act(async () => {
            await Promise.resolve();
        });
        expect(listProviders).not.toHaveBeenCalled();
    });

    it('passes the filter options to the API', async () => {
        const { result } = renderHook(() =>
            useLlmProviders(client, { enabled: true, includeModelCount: true })
        );

        await waitFor(() => expect(result.current.providers).toHaveLength(2));
        expect(listProviders).toHaveBeenCalledWith({ enabled: true, includeModelCount: true });
    });

    it('does not re-fetch when a re-render passes an equivalent options object', async () => {
        const { result, rerender } = renderHook(
            ({ enabled }: { enabled: boolean }) => useLlmProviders(client, { enabled }),
            { initialProps: { enabled: true } }
        );

        await waitFor(() => expect(result.current.providers).toHaveLength(2));
        expect(listProviders).toHaveBeenCalledTimes(1);

        // A new object literal with the same contents — what every render of
        // the consuming form produces.
        rerender({ enabled: true });
        rerender({ enabled: true });

        await act(async () => {
            await Promise.resolve();
        });
        expect(listProviders).toHaveBeenCalledTimes(1);
    });

    it('re-fetches when the filter actually changes', async () => {
        const { rerender } = renderHook(
            ({ enabled }: { enabled: boolean }) => useLlmProviders(client, { enabled }),
            { initialProps: { enabled: true } }
        );

        await waitFor(() => expect(listProviders).toHaveBeenCalledTimes(1));

        rerender({ enabled: false });

        await waitFor(() => expect(listProviders).toHaveBeenCalledTimes(2));
        expect(listProviders).toHaveBeenLastCalledWith({ enabled: false });
    });

    it('serves a remount from cache without hitting the API again', async () => {
        const first = renderHook(() => useLlmProviders(client));
        await waitFor(() => expect(first.result.current.providers).toHaveLength(2));
        first.unmount();

        const second = renderHook(() => useLlmProviders(client));

        await waitFor(() => expect(second.result.current.providers).toHaveLength(2));
        expect(listProviders).toHaveBeenCalledTimes(1);
    });

    it('does not share a cache entry across clients', async () => {
        const first = renderHook(() => useLlmProviders(client));
        await waitFor(() => expect(first.result.current.providers).toHaveLength(2));

        // Switching tenants recreates the client; its cache must be its own.
        const otherList = vi
            .fn()
            .mockResolvedValue({ providers: [provider('bedrock')], total: 1 });
        const otherClient = makeClient(otherList);
        const second = renderHook(() => useLlmProviders(otherClient));

        await waitFor(() => expect(second.result.current.providers).toHaveLength(1));
        expect(second.result.current.providers[0].key).toBe('bedrock');
        expect(otherList).toHaveBeenCalledTimes(1);
    });

    it('refresh() bypasses the cache and picks up newly added providers', async () => {
        const { result } = renderHook(() => useLlmProviders(client));
        await waitFor(() => expect(result.current.providers).toHaveLength(2));

        listProviders.mockResolvedValue({
            providers: [provider('anthropic'), provider('openai'), provider('bedrock')],
            total: 3,
        });

        await act(async () => {
            await result.current.refresh();
        });

        expect(listProviders).toHaveBeenCalledTimes(2);
        expect(result.current.providers.map((p) => p.key)).toContain('bedrock');
    });

    it('a refreshed result becomes the cached one for the next mount', async () => {
        const { result, unmount } = renderHook(() => useLlmProviders(client));
        await waitFor(() => expect(result.current.providers).toHaveLength(2));

        listProviders.mockResolvedValue({ providers: [provider('bedrock')], total: 1 });
        await act(async () => {
            await result.current.refresh();
        });
        unmount();

        const second = renderHook(() => useLlmProviders(client));

        await waitFor(() => expect(second.result.current.providers).toHaveLength(1));
        expect(listProviders).toHaveBeenCalledTimes(2);
    });

    it('surfaces a fetch failure as an Error and stops loading', async () => {
        listProviders.mockRejectedValue(new Error('providers endpoint down'));

        const { result } = renderHook(() => useLlmProviders(client));

        await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
        expect(result.current.error?.message).toBe('providers endpoint down');
        expect(result.current.loading).toBe(false);
        expect(result.current.providers).toEqual([]);
    });

    it('wraps a non-Error rejection so consumers always get a message', async () => {
        listProviders.mockRejectedValue('gateway refused');

        const { result } = renderHook(() => useLlmProviders(client));

        await waitFor(() => expect(result.current.error).toBeInstanceOf(Error));
        expect(result.current.error?.message).toBe('gateway refused');
    });

    it('keeps the loaded list when a refresh fails', async () => {
        const { result } = renderHook(() => useLlmProviders(client));
        await waitFor(() => expect(result.current.providers).toHaveLength(2));

        listProviders.mockRejectedValue(new Error('providers endpoint down'));
        await act(async () => {
            await result.current.refresh();
        });

        // Blanking the provider dropdown mid-edit would lose the operator's
        // selection over a transient failure.
        expect(result.current.providers).toHaveLength(2);
        expect(result.current.error?.message).toBe('providers endpoint down');
    });

    it('clears a previous error on a successful refresh', async () => {
        listProviders.mockRejectedValueOnce(new Error('providers endpoint down'));
        const { result } = renderHook(() => useLlmProviders(client));
        await waitFor(() => expect(result.current.error).not.toBeNull());

        listProviders.mockResolvedValue({ providers: [provider('anthropic')], total: 1 });
        await act(async () => {
            await result.current.refresh();
        });

        expect(result.current.error).toBeNull();
        expect(result.current.providers).toHaveLength(1);
    });
});
