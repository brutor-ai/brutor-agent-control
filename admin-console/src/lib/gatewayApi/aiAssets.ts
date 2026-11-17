/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The AI asset register, obligation frameworks and compliance profiles.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';
import type {
    AIAsset,
    AIAssetCreateRequest,
    AssetKind,
    AssetStatus,
    LifecycleStage,
    RiskTier,
} from '../aiAssetTypes';
import { GatewayApiBase } from './base';

export class AiAssetsApi extends GatewayApiBase {

    // ============================================================================
    // AI ASSETS (AI Asset Registry — governance inventory of governed AI assets)
    // ============================================================================

    async listAIAssets(params?: {
        asset_kind?: AssetKind;
        lifecycle_stage?: LifecycleStage;
        risk_tier?: RiskTier;
        status?: AssetStatus;
        q?: string;
        limit?: number;
        offset?: number;
    }) {
        const qs = new URLSearchParams();
        if (params?.asset_kind) qs.set('asset_kind', params.asset_kind);
        if (params?.lifecycle_stage) qs.set('lifecycle_stage', params.lifecycle_stage);
        if (params?.risk_tier) qs.set('risk_tier', params.risk_tier);
        if (params?.status) qs.set('status', params.status);
        if (params?.q) qs.set('q', params.q);
        if (params?.limit !== undefined) qs.set('limit', String(params.limit));
        if (params?.offset !== undefined) qs.set('offset', String(params.offset));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<{ assets: AIAsset[]; total: number }>(
            `/v1/admin/assets${suffix}`,
            { method: 'GET' },
        );
    }

    async getAIAsset(id: string) {
        return this.request<AIAsset>(`/v1/admin/assets/${id}`, {
            method: 'GET',
        });
    }

    async createAIAsset(body: AIAssetCreateRequest) {
        return this.request<AIAsset>(`/v1/admin/assets`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    /** Obligation frameworks catalog (jurisdiction-neutral). */
    async listObligationFrameworks() {
        return this.request<import('../obligationTypes').ObligationFrameworkInfo[]>(
            '/v1/admin/compliance/obligation-frameworks',
        );
    }

    /** Tenant obligation board — every obligation with its date and the
     *  systems in scope. Applicability is computed, dated, never stored. */
    async fetchObligationsBoard(framework = 'eu-ai-act') {
        return this.request<import('../obligationTypes').ObligationsBoardResponse>(
            `/v1/admin/compliance/obligations?framework=${encodeURIComponent(framework)}`,
        );
    }

    /** One AI System's obligations under a framework. */
    async fetchSystemObligations(systemId: string, framework = 'eu-ai-act') {
        return this.request<import('../obligationTypes').SystemObligationsResponse>(
            `/v1/admin/ai-systems/${systemId}/obligations?framework=${encodeURIComponent(framework)}`,
        );
    }

    // =========================================================================
    // Compliance framework registry + AI System compliance profiles. The registry is data; enablement is per tenant.
    // =========================================================================

    async listComplianceFrameworks(): Promise<ApiResponse<'/v1/admin/compliance/frameworks', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/frameworks', 'get'>>(
            `/v1/admin/compliance/frameworks`,
        );
    }

    async setComplianceFrameworkEnabled(
        frameworkId: string,
        enabled: boolean,
    ): Promise<ApiResponse<'/v1/admin/compliance/frameworks/{framework_id}', 'patch'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/frameworks/{framework_id}', 'patch'>>(
            `/v1/admin/compliance/frameworks/${encodeURIComponent(frameworkId)}`,
            { method: 'PATCH', body: JSON.stringify({ enabled } satisfies ApiRequestBody<'/v1/admin/compliance/frameworks/{framework_id}', 'patch'>) },
        );
    }

    async getAISystemComplianceProfile(
        aiSystemId: string,
    ): Promise<ApiResponse<'/v1/admin/compliance/ai-systems/{ai_system_id}/profile', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/ai-systems/{ai_system_id}/profile', 'get'>>(
            `/v1/admin/compliance/ai-systems/${encodeURIComponent(aiSystemId)}/profile`,
        );
    }

    async putAISystemComplianceProfile<Sent>(
        aiSystemId: string,
        body: ApiBodyExactly<'/v1/admin/compliance/ai-systems/{ai_system_id}/profile', 'put', Sent>,
    ): Promise<ApiResponse<'/v1/admin/compliance/ai-systems/{ai_system_id}/profile', 'put'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/ai-systems/{ai_system_id}/profile', 'put'>>(
            `/v1/admin/compliance/ai-systems/${encodeURIComponent(aiSystemId)}/profile`,
            { method: 'PUT', body: JSON.stringify(body) },
        );
    }

    /**
     * Resolve an icon/asset reference for use directly in an `<img src>`.
     * Absolute URLs (http/https/data) pass through unchanged; a
     * leading-slash API path (e.g. the Asset Registry's
     * `/v1/admin/mcp-servers/{id}/icon`) is prefixed with the gateway base
     * URL so it loads from the control plane rather than the admin UI
     * origin. Anything else (e.g. a `lucide:` sentinel) is returned as-is
     * for the caller to special-case.
     */
    resolveAssetUrl(ref: string): string {
        if (/^(https?:|data:)/i.test(ref)) return ref;
        if (ref.startsWith('/')) return `${this.baseUrl}${ref}`;
        return ref;
    }
}
