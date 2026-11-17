/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLM models, the global catalog, search and model access by group.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import {
    LLModelConfig,
    LLModelCreateRequest,
    LLModelListResponse,
    HealthCheckResponse,
    LLModelSearchRequest,
    LLModelSearchResponse,
    LLModelSearchWithSummaryResponse,
    ModelSearchSuggestion,
    LLMCatalogEntry,
    LLMCatalogListResponse,
    LLMCatalogImportRequest,
    LLMCatalogCreateRequest,
} from '../llmTypes';
import { GatewayApiBase, BULK_HEALTH_CHECK_TIMEOUT_MS, healthCheckTimeoutMs } from './base';
import type { ApiBodyExactly, ApiQuery } from '../api/controlPlaneTypes';

export class LlmModelsApi extends GatewayApiBase {

    // ============================================================================
    // LLM Server Management Methods
    // ============================================================================

    /**
     * List all LLMs
     */
    async listLLMs(query?: Pick<NonNullable<ApiQuery<'/v1/admin/llms', 'get'>>, 'mode' | 'enabled' | 'provider'>): Promise<LLModelListResponse> {
        const params = new URLSearchParams();
        if (query?.mode) params.append('mode', query.mode);
        if (query?.enabled !== undefined && query.enabled !== null) params.append('enabled', String(query.enabled));
        if (query?.provider) params.append('provider', query.provider);
        const qs = params.toString();
        return this.request<LLModelListResponse>(`/v1/admin/llms${qs ? `?${qs}` : ''}`, {
            method: 'GET'
        });
    }

    /**
     * Get a specific LLM by ID
     */
    async getLLM(modelId: string): Promise<LLModelConfig> {
        return this.request<LLModelConfig>(`/v1/admin/llms/${modelId}`, {
            method: 'GET'
        });
    }

    /**
     * Create a new LLM config
     */
    async createLLM(model: LLModelCreateRequest): Promise<LLModelConfig> {
        return this.request<LLModelConfig>('/v1/admin/llms', {
            method: 'POST',
            body: JSON.stringify(model)
        });
    }

    /**
     * Duplicate an existing LLM config. Performed server-side so the
     * encrypted api_key and every column on the source row can be
     * carried over without the browser ever seeing secrets.
     *
     * Returns the created model (same shape as /admin/llms/{id}
     * detail, under the `model` key).
     */
    async copyLLM(modelId: string): Promise<{ model: LLModelConfig }> {
        return this.request<{ model: LLModelConfig }>(
            `/v1/admin/llms/${encodeURIComponent(modelId)}/copy`,
            { method: 'POST' },
        );
    }

    /**
     * Update an existing LLM config
     */
    async updateLLM<Sent>(modelId: string, updates: ApiBodyExactly<'/v1/admin/llms/{model_id}', 'patch', Sent>): Promise<LLModelConfig> {
        return this.request<LLModelConfig>(`/v1/admin/llms/${modelId}`, {
            method: 'PATCH',
            body: JSON.stringify(updates)
        });
    }

    /**
     * Delete an LLM config
     */
    async deleteLLM(modelId: string): Promise<void> {
        return this.request<void>(`/v1/admin/llms/${modelId}`, {
            method: 'DELETE'
        });
    }

    /**
     * Test connection to an LLM.
     *
     * `mode` lets the caller widen the fetch timeout for slow
     * modalities. The default 30 s ceiling is fine for chat and
     * embedding probes (a single completions / embeddings round-
     * trip) but trips on image (DALL-E 3, gpt-image-1, Flux,
     * Imagen, Stability — routinely 30-90 s), audio (TTS / Whisper
     * — up to ~90 s for long inputs), and video (multi-minute
     * generation jobs). Without the override the in-browser fetch
     * aborts long before the Python admin's per-model `timeout`
     * field (typically 60-300 s, up to 1800 s for video) has had a
     * chance to return.
     */
    async testLLMConnection(
        modelId: string,
        mode?: string | null,
    ): Promise<HealthCheckResponse> {
        return this.request<HealthCheckResponse>(`/v1/admin/llms/${modelId}/health-check`, {
            method: 'POST',
            body: JSON.stringify({}),
            timeout: healthCheckTimeoutMs(mode),
        });
    }

    /**
     * Trigger health check for all LLMs. The server fans out
     * probes across every keyed model regardless of mode, so the
     * fetch needs to outlast the slowest one — match the video
     * envelope (15 min) so a single slow Imagen / Sora row doesn't
     * kill the bulk run.
     *
     * `probe` picks the cost profile: `catalog` (background sweeps) hits
     * each provider's free /models listing and lets the server back off
     * persistently misconfigured models; `endpoint` (default, operator
     * click) runs the real generation probe per model.
     */
    async bulkHealthCheckLLMs(
        options: { probe?: 'catalog' | 'endpoint' } = {},
    ): Promise<void> {
        return this.request<void>('/v1/admin/llms/health-check/bulk', {
            method: 'POST',
            body: JSON.stringify({ probe: options.probe ?? 'endpoint' }),
            timeout: BULK_HEALTH_CHECK_TIMEOUT_MS,
        });
    }

    // ============================================================================
    // Global LLM Catalog Methods
    // ============================================================================

    //
    // The catalog (`llm_catalog`) is the GLOBAL browseable list of models an
    // operator can import. Distinct from the configured per-tenant instances
    // managed by listLLMs/createLLM/updateLLM/deleteLLM. Importing an entry
    // materialises a configured `llm_model` instance.

    /**
     * Browse / search the global LLM catalog.
     */
    async listLLMCatalog(params?: {
        provider_key?: string;
        mode?: string;
        recommended?: boolean;
        q?: string;
        limit?: number;
        offset?: number;
    }): Promise<LLMCatalogListResponse> {
        const query = new URLSearchParams();
        if (params?.provider_key) query.set('provider_key', params.provider_key);
        if (params?.mode) query.set('mode', params.mode);
        if (params?.recommended !== undefined) query.set('recommended', String(params.recommended));
        if (params?.q) query.set('q', params.q);
        if (params?.limit !== undefined) query.set('limit', String(params.limit));
        if (params?.offset !== undefined) query.set('offset', String(params.offset));
        const qs = query.toString();
        return this.request<LLMCatalogListResponse>(
            `/v1/admin/llm-catalog${qs ? `?${qs}` : ''}`,
            { method: 'GET' },
        );
    }

    /**
     * Get a single catalog entry by id.
     */
    async getLLMCatalogEntry(id: string): Promise<LLMCatalogEntry> {
        return this.request<LLMCatalogEntry>(
            `/v1/admin/llm-catalog/${encodeURIComponent(id)}`,
            { method: 'GET' },
        );
    }

    /**
     * Import a catalog entry into the tenant as a configured `llm_model`
     * instance. Returns the created model (same shape as createLLM /
     * getLLM — the backend wraps it under a `model` key like copyLLM, so
     * we unwrap here for caller convenience).
     */
    async importLLMCatalogModel(
        catalogId: string,
        overrides?: LLMCatalogImportRequest,
    ): Promise<LLModelConfig> {
        const resp = await this.request<{ model: LLModelConfig }>(
            `/v1/admin/llm-catalog/${encodeURIComponent(catalogId)}/import`,
            {
                method: 'POST',
                body: JSON.stringify(overrides ?? {}),
            },
        );
        return resp.model;
    }

    /**
     * Create a custom global catalog entry (system-admin only).
     */
    async createLLMCatalogEntry(
        body: LLMCatalogCreateRequest,
    ): Promise<LLMCatalogEntry> {
        return this.request<LLMCatalogEntry>(
            '/v1/admin/llm-catalog',
            {
                method: 'POST',
                body: JSON.stringify(body),
            },
        );
    }

    /**
     * Update a global catalog entry (system-admin only). Partial PATCH —
     * provider_key / model_name are immutable and not sent.
     */
    async updateLLMCatalogEntry<Sent>(
        id: string,
        body: ApiBodyExactly<'/v1/admin/llm-catalog/{catalog_id}', 'patch', Sent>,
    ): Promise<LLMCatalogEntry> {
        return this.request<LLMCatalogEntry>(
            `/v1/admin/llm-catalog/${encodeURIComponent(id)}`,
            {
                method: 'PATCH',
                body: JSON.stringify(body),
            },
        );
    }

    /**
     * Delete a global catalog entry (system-admin only).
     */
    async deleteLLMCatalogEntry(id: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/llm-catalog/${encodeURIComponent(id)}`,
            { method: 'DELETE' },
        );
    }

    // ============================================================================
    // LLM Model Search Methods
    // ============================================================================

    /**
     * Search for LLM models with advanced filtering
     * @param searchRequest - Search criteria and pagination
     * @returns Search results with pagination metadata
     */
    async searchLLMs(searchRequest: LLModelSearchRequest): Promise<LLModelSearchResponse> {
        return this.request<LLModelSearchResponse>('/v1/admin/llms/search', {
            method: 'POST',
            body: JSON.stringify(searchRequest)
        });
    }

    /**
     * Search for LLM models with summary statistics
     * @param searchRequest - Search criteria and pagination
     * @returns Search results with summary statistics
     */
    async searchLLMsWithSummary(searchRequest: LLModelSearchRequest): Promise<LLModelSearchWithSummaryResponse> {
        return this.request<LLModelSearchWithSummaryResponse>('/v1/admin/llms/search/with-summary', {
            method: 'POST',
            body: JSON.stringify(searchRequest)
        });
    }

    /**
     * Quick search using query parameters
     * @param params - Query parameters for search
     * @returns Search results
     */
    async quickSearchLLMs(params: {
        name?: string;
        provider?: string;
        streaming?: boolean;
        vision?: boolean;
        functions?: boolean;
        max_cost?: number;
        enabled_only?: boolean;
        offset?: number;
        limit?: number;
        sort_by?: string;
        sort_order?: string;
    }): Promise<LLModelSearchResponse> {
        const queryParams = new URLSearchParams();

        Object.entries(params).forEach(([key, value]) => {
            if (value !== undefined && value !== null) {
                queryParams.append(key, String(value));
            }
        });

        return this.request<LLModelSearchResponse>(`/v1/admin/llms/search/quick?${queryParams.toString()}`, {
            method: 'GET'
        });
    }

    /**
     * Get autocomplete suggestions for a field
     * @param field - Field to get suggestions for (provider, model_name, name, tags)
     * @param prefix - Optional prefix to filter by
     * @param limit - Maximum number of suggestions
     * @returns Autocomplete suggestions
     */
    async getLLMSearchSuggestions(
        field: string,
        prefix?: string,
        limit: number = 20
    ): Promise<ModelSearchSuggestion> {
        const queryParams = new URLSearchParams({ field, limit: String(limit) });
        if (prefix) {
            queryParams.append('prefix', prefix);
        }

        return this.request<ModelSearchSuggestion>(`/v1/admin/llms/search/suggestions?${queryParams.toString()}`, {
            method: 'GET'
        });
    }

    // ============================================================================
    // LLM Group Management Methods
    // ============================================================================

    /**
     * Get models accessible to a specific user
     * @param userId - User ID (optional, defaults to current user)
     */
    async getUserAccessibleModels(userId?: string): Promise<{
        user_id: string;
        user_groups: string[];
        models: LLModelConfig[];
        total: number;
    }> {
        const endpoint = userId
            ? `/v1/admin/llms/user/${userId}/accessible`
            : `/v1/admin/llms/user/me/accessible`;

        return this.request(endpoint, {
            method: 'GET'
        });
    }

    /**
     * Get models by group
     * @param groupId - Group ID (required - use 'default' for the Default group)
     */
    async getModelsByGroup(groupId: string): Promise<{
        group_id: string;
        group_name?: string;
        models: LLModelConfig[];
        total: number;
    }> {
        const endpoint = `/v1/admin/llms/group/${groupId}`;

        return this.request(endpoint, {
            method: 'GET'
        });
    }
}
