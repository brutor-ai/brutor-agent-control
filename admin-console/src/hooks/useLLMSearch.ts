/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import { useState, useCallback } from 'react';
import { useAuth } from './useAuth';
import { LLMCatalogListResponse } from '../lib/llmTypes';

/**
 * Filters accepted by the global LLM catalog browse query. Maps 1:1 to
 * the `/v1/admin/llm-catalog` query params.
 */
export interface LLMCatalogSearchParams {
    provider_key?: string;
    mode?: string;
    recommended?: boolean;
    q?: string;
    limit?: number;
    offset?: number;
}

/**
 * Hook for browsing / searching the GLOBAL LLM catalog (`llm_catalog`).
 *
 * Distinct from the configured per-tenant model list (`listLLMs`): the
 * catalog is the source operators browse to *import* a model. Importing
 * an entry materialises a configured instance — see
 * `gatewayClient.importLLMCatalogModel`.
 */
export const useLLMSearch = () => {
    const { gatewayClient } = useAuth();

    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [searchResults, setSearchResults] = useState<LLMCatalogListResponse | null>(null);

    /**
     * Browse the catalog with filters.
     */
    const search = useCallback(async (
        params: LLMCatalogSearchParams,
    ): Promise<LLMCatalogListResponse> => {
        setLoading(true);
        setError(null);

        try {
            const results = await gatewayClient.listLLMCatalog(params);
            setSearchResults(results);
            return results;
        } catch (err) {
            const errorMessage = err instanceof Error ? err.message : 'Failed to search the model catalog';
            setError(errorMessage);
            throw err;
        } finally {
            setLoading(false);
        }
    }, [gatewayClient]);

    /**
     * Clear search results.
     */
    const clearResults = useCallback(() => {
        setSearchResults(null);
        setError(null);
    }, []);

    return {
        search,
        clearResults,
        loading,
        error,
        searchResults,
    };
};
