/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * LLM providers and their catalog.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import { GatewayApiBase } from './base';
import type { ApiBodyExactly } from '../api/controlPlaneTypes';

export class LlmProvidersApi extends GatewayApiBase {

    // ============================================================================
    // LLM Provider Management Methods
    // ----------------------------------------------------------------------

    //
    // Providers are first-class entities behind `/v1/admin/providers`.
    // The admin UI reads from this endpoint at mount time instead of
    // carrying a hardcoded `PROVIDERS` constant — adding / renaming /
    // disabling a provider shows up everywhere without a rebuild.
    // See `lib/llmProviderTypes.ts` for the wire shapes.
    // ============================================================================

    async listProviders(
        opts?: import('../llmProviderTypes').LlmProviderListOptions,
    ): Promise<import('../llmProviderTypes').LlmProviderListResponse> {
        const params = new URLSearchParams();
        if (opts?.category) params.set('category', opts.category);
        if (opts?.enabled !== undefined) {
            params.set('enabled', String(opts.enabled));
        }
        if (opts?.includeModelCount) {
            params.set('include_model_count', 'true');
        }
        const qs = params.toString();
        const path = qs ? `/v1/admin/providers?${qs}` : '/v1/admin/providers';
        return this.request<
            import('../llmProviderTypes').LlmProviderListResponse
        >(path, { method: 'GET' });
    }

    /**
     * Browse the GLOBAL provider catalog (`GET /v1/admin/provider-catalog`).
     *
     * Returns every provider the platform knows about — not just the
     * per-tenant *configured* ones from `listProviders()`. Used to populate
     * the model-form provider dropdown so any provider is selectable; the
     * backend auto-imports the chosen provider into `llm_providers` when a
     * model is created with its `key`.
     */
    async listProviderCatalog(): Promise<
        import('../llmProviderTypes').LlmProviderCatalogListResponse
    > {
        return this.request<
            import('../llmProviderTypes').LlmProviderCatalogListResponse
        >('/v1/admin/provider-catalog', { method: 'GET' });
    }

    async getProvider(
        providerId: string,
    ): Promise<import('../llmProviderTypes').LlmProviderResponse> {
        return this.request<
            import('../llmProviderTypes').LlmProviderResponse
        >(`/v1/admin/providers/${encodeURIComponent(providerId)}`, {
            method: 'GET',
        });
    }

    async createProvider(
        body: import('../llmProviderTypes').LlmProviderCreateRequest,
    ): Promise<import('../llmProviderTypes').LlmProviderResponse> {
        return this.request<
            import('../llmProviderTypes').LlmProviderResponse
        >('/v1/admin/providers', {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    async updateProvider<Sent>(
        providerId: string,
        body: ApiBodyExactly<'/v1/admin/providers/{provider_id}', 'patch', Sent>,
    ): Promise<import('../llmProviderTypes').LlmProviderResponse> {
        return this.request<
            import('../llmProviderTypes').LlmProviderResponse
        >(`/v1/admin/providers/${encodeURIComponent(providerId)}`, {
            method: 'PATCH',
            body: JSON.stringify(body),
        });
    }

    async deleteProvider(providerId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/providers/${encodeURIComponent(providerId)}`,
            { method: 'DELETE' },
        );
    }

    /** Rotate the slug. The endpoint refuses if any model still
     * references this provider — reassign or delete those models
     * first. */
    async renameProvider(
        providerId: string,
        newKey: string,
    ): Promise<import('../llmProviderTypes').LlmProviderResponse> {
        const params = new URLSearchParams({ new_key: newKey });
        return this.request<
            import('../llmProviderTypes').LlmProviderResponse
        >(
            `/v1/admin/providers/${encodeURIComponent(providerId)}/rename?${params.toString()}`,
            { method: 'POST' },
        );
    }

    /** URL of the provider icon endpoint. Returns the bare URL —
     *  callers point an `<img src=…>` at it. The endpoint serves
     *  binary bytes with the right Content-Type, so the browser
     *  renders SVG / PNG / etc. without further work.
     *
     *  We append the upload timestamp as a query param so an icon
     *  swap busts the browser cache without waiting for the ETag
     *  round-trip. The endpoint ignores the query param.
     */
    getProviderIconUrl(
        providerId: string,
        iconUploadedAt?: string | null,
    ): string {
        const base = `${this.baseUrl}/v1/admin/providers/${encodeURIComponent(providerId)}/icon`;
        if (!iconUploadedAt) return base;
        const v = encodeURIComponent(iconUploadedAt);
        return `${base}?v=${v}`;
    }

    async uploadProviderIcon(
        providerId: string,
        file: File,
    ): Promise<import('../llmProviderTypes').LlmProviderResponse> {
        const form = new FormData();
        form.append('file', file);
        // `request()` leaves Content-Type alone for FormData bodies so the
        // browser can set the multipart boundary, so no bare `fetch` is needed.
        // Going through the client gives the upload the expiry pre-check and
        // the 401 → session-clear path: a bare fetch with a stale token would
        // throw a raw "uploadProviderIcon failed: 401" and leave the session
        // untouched.
        // Tenant context is resolved from the JWT claims server-side; no
        // explicit X-Tenant-ID needed for admin-scoped requests.
        return this.request<import('../llmProviderTypes').LlmProviderResponse>(
            `/v1/admin/providers/${encodeURIComponent(providerId)}/icon`,
            {
                method: 'POST',
                body: form,
            },
        );
    }

    async deleteProviderIcon(providerId: string): Promise<void> {
        await this.request<void>(
            `/v1/admin/providers/${encodeURIComponent(providerId)}/icon`,
            { method: 'DELETE' },
        );
    }
}
