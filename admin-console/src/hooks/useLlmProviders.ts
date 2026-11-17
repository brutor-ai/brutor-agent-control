/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/*
 * Hook to fetch the tenant's `LlmProvider` catalog from
 * `/v1/admin/providers`, so that no form carries a hardcoded provider
 * list.
 *
 * Caching strategy:
 *
 *   * One in-flight fetch per (client identity, options) pair.
 *   * Results memoised in a `WeakMap<GatewayApiClient, …>` so a
 *     re-render of `LLModelForm` doesn't re-hit the API. The
 *     WeakMap auto-clears when the client is GC'd (i.e. when the
 *     operator switches tenants and the client is recreated).
 *   * `refresh()` invalidates the cache entry and re-fetches —
 *     used after the operator edits providers in the
 *     Providers admin page.
 */
import { useCallback, useEffect, useRef, useState } from 'react';

import type { GatewayApiClient } from '../lib/gatewayApiClient';
import type {
    LlmProviderListOptions,
    LlmProviderResponse,
} from '../lib/llmProviderTypes';

interface CacheEntry {
    providers: LlmProviderResponse[];
    fetchedAt: number;
}

// Stale-while-revalidate window — providers are operator-managed,
// not real-time, so a minute is plenty.
const CACHE_TTL_MS = 60_000;

const cache: WeakMap<GatewayApiClient, Map<string, CacheEntry>> = new WeakMap();

function optsKey(opts?: LlmProviderListOptions): string {
    if (!opts) return '';
    return JSON.stringify({
        c: opts.category ?? null,
        e: opts.enabled ?? null,
        m: opts.includeModelCount ?? false,
    });
}

function getCached(
    client: GatewayApiClient,
    key: string,
): CacheEntry | undefined {
    return cache.get(client)?.get(key);
}

function setCached(
    client: GatewayApiClient,
    key: string,
    entry: CacheEntry,
): void {
    let bucket = cache.get(client);
    if (!bucket) {
        bucket = new Map();
        cache.set(client, bucket);
    }
    bucket.set(key, entry);
}

export interface UseLlmProvidersResult {
    providers: LlmProviderResponse[];
    loading: boolean;
    error: Error | null;
    refresh: () => Promise<void>;
}

export function useLlmProviders(
    api: GatewayApiClient | null,
    opts?: LlmProviderListOptions,
): UseLlmProvidersResult {
    const key = optsKey(opts);
    const [providers, setProviders] = useState<LlmProviderResponse[]>(() => {
        if (!api) return [];
        return getCached(api, key)?.providers ?? [];
    });
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<Error | null>(null);

    // `opts` is a fresh object literal at every call site, so depending on its
    // identity would re-fetch on every render. `key` is its serialised form and
    // is the real dependency; this ref carries the object itself through to the
    // request without widening the dependency array.
    const optsRef = useRef(opts);
    useEffect(() => {
        optsRef.current = opts;
    });

    const fetchOnce = useCallback(
        async (force: boolean) => {
            if (!api) return;
            const hit = getCached(api, key);
            if (
                !force
                && hit
                && Date.now() - hit.fetchedAt < CACHE_TTL_MS
            ) {
                setProviders(hit.providers);
                return;
            }
            setLoading(true);
            setError(null);
            try {
                const resp = await api.listProviders(optsRef.current);
                setCached(api, key, {
                    providers: resp.providers,
                    fetchedAt: Date.now(),
                });
                setProviders(resp.providers);
            } catch (err) {
                setError(
                    err instanceof Error
                        ? err
                        : new Error(String(err)),
                );
            } finally {
                setLoading(false);
            }
        },
        [api, key],
    );

    useEffect(() => {
        // eslint-disable-next-line react-hooks/set-state-in-effect -- fetching the provider catalog on mount IS the external-system sync this hook exists for; the setState happens in the promise continuation, and the warm-cache branch is a deliberate synchronous rehydrate.
        void fetchOnce(false);
    }, [fetchOnce]);

    const refresh = useCallback(() => fetchOnce(true), [fetchOnce]);

    return { providers, loading, error, refresh };
}
