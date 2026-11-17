/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The evidence ledger: records, logs, heads, receipts and the evidence settings.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';
import { normalizeVerifyResult } from '../complianceEvidenceTypes';
import { normalizeBundleBuild, normalizeBundleVerify } from '../evidenceSurfaceTypes';
import { GatewayApiBase } from './base';

export class EvidenceApi extends GatewayApiBase {

    // =========================================================================
    // Evidence ledger. Records, logs, heads and receipts
    // are written by the core; this client only reads them. Witnesses are
    // configuration and are written here.
    // =========================================================================

    async listEvidenceRecords(
        filters: import('../complianceEvidenceTypes').EvidenceRecordFilters = {},
    ): Promise<ApiResponse<'/v1/admin/evidence/records', 'get'>> {
        const qs = new URLSearchParams();
        for (const [key, value] of Object.entries(filters)) {
            if (value !== undefined && value !== null && value !== '') qs.set(key, String(value));
        }
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<ApiResponse<'/v1/admin/evidence/records', 'get'>>(
            `/v1/admin/evidence/records${suffix}`,
        );
    }

    async getEvidenceRecord(
        recordId: string,
    ): Promise<ApiResponse<'/v1/admin/evidence/records/{record_id}', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/records/{record_id}', 'get'>>(
            `/v1/admin/evidence/records/${encodeURIComponent(recordId)}`,
        );
    }

    async getEvidenceRecordProof(
        recordId: string,
    ): Promise<ApiResponse<'/v1/admin/evidence/records/{record_id}/proof', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/records/{record_id}/proof', 'get'>>(
            `/v1/admin/evidence/records/${encodeURIComponent(recordId)}/proof`,
        );
    }

    async getEvidenceSummary(
        filters: import('../complianceEvidenceTypes').EvidenceSummaryFilters = {},
    ): Promise<ApiResponse<'/v1/admin/evidence/summary', 'get'>> {
        const qs = new URLSearchParams();
        for (const [key, value] of Object.entries(filters)) {
            if (value !== undefined && value !== null && value !== '') qs.set(key, String(value));
        }
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<ApiResponse<'/v1/admin/evidence/summary', 'get'>>(
            `/v1/admin/evidence/summary${suffix}`,
        );
    }

    async listEvidenceLogs(): Promise<ApiResponse<'/v1/admin/evidence/logs', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/logs', 'get'>>(`/v1/admin/evidence/logs`);
    }

    async listEvidenceTreeHeads(
        logId: string,
        params: { limit?: number; offset?: number } = {},
    ): Promise<ApiResponse<'/v1/admin/evidence/logs/{log_id}/tree-heads', 'get'>> {
        const qs = new URLSearchParams();
        if (params.limit !== undefined) qs.set('limit', String(params.limit));
        if (params.offset !== undefined) qs.set('offset', String(params.offset));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<ApiResponse<'/v1/admin/evidence/logs/{log_id}/tree-heads', 'get'>>(
            `/v1/admin/evidence/logs/${encodeURIComponent(logId)}/tree-heads${suffix}`,
        );
    }

    async getEvidenceTreeHead(
        headId: string,
    ): Promise<ApiResponse<'/v1/admin/evidence/tree-heads/{head_id}', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/tree-heads/{head_id}', 'get'>>(
            `/v1/admin/evidence/tree-heads/${encodeURIComponent(headId)}`,
        );
    }

    async listEvidenceWitnesses(): Promise<ApiResponse<'/v1/admin/evidence/witnesses', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/witnesses', 'get'>>(
            `/v1/admin/evidence/witnesses`,
        );
    }

    async createEvidenceWitness(
        body: ApiRequestBody<'/v1/admin/evidence/witnesses', 'post'>,
    ): Promise<ApiResponse<'/v1/admin/evidence/witnesses', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/witnesses', 'post'>>(
            `/v1/admin/evidence/witnesses`,
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    async updateEvidenceWitness<Sent>(
        witnessId: string,
        body: ApiBodyExactly<'/v1/admin/evidence/witnesses/{witness_id}', 'patch', Sent>,
    ): Promise<ApiResponse<'/v1/admin/evidence/witnesses/{witness_id}', 'patch'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/witnesses/{witness_id}', 'patch'>>(
            `/v1/admin/evidence/witnesses/${encodeURIComponent(witnessId)}`,
            { method: 'PATCH', body: JSON.stringify(body) },
        );
    }

    async deleteEvidenceWitness(
        witnessId: string,
    ): Promise<ApiResponse<'/v1/admin/evidence/witnesses/{witness_id}', 'delete'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/witnesses/{witness_id}', 'delete'>>(
            `/v1/admin/evidence/witnesses/${encodeURIComponent(witnessId)}`,
            { method: 'DELETE' },
        );
    }

    async listEvidenceDisclosures(
        params: { limit?: number; offset?: number } = {},
    ): Promise<ApiResponse<'/v1/admin/evidence/disclosures', 'get'>> {
        const qs = new URLSearchParams();
        if (params.limit !== undefined) qs.set('limit', String(params.limit));
        if (params.offset !== undefined) qs.set('offset', String(params.offset));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<ApiResponse<'/v1/admin/evidence/disclosures', 'get'>>(
            `/v1/admin/evidence/disclosures${suffix}`,
        );
    }

    /** Verify a stored record via the core. The core holds the key and the
     *  vectors; an unreachable core comes back as a finding, never as ok. */
    async verifyEvidenceRecord(
        recordId: string,
    ): Promise<import('../complianceEvidenceTypes').EvidenceVerifyResult> {
        const raw = await this.request<unknown>(`/v1/admin/evidence/verify`, {
            method: 'POST',
            body: JSON.stringify({ record_id: recordId }),
        });
        return normalizeVerifyResult(raw);
    }

    // =========================================================================
    // Evidence surfaces (run details, Settings → Evidence, Audit
    // Chains, blind review queue, AI System → Compliance). Bundles and their
    // verification are built by the core and relayed by the CP.
    // =========================================================================

    /** Heads of one tenant's log. `tenantId` lets a system admin (Audit
     *  Chains) address a tenant; a tenant admin may pass its own. */
    async listEvidenceLogTreeHeads(
        logId: string,
        params: { tenantId?: string; limit?: number } = {},
    ): Promise<ApiResponse<'/v1/admin/evidence/logs/{log_id}/tree-heads', 'get'>> {
        const qs = new URLSearchParams();
        if (params.tenantId) qs.set('tenant_id', params.tenantId);
        if (params.limit !== undefined) qs.set('limit', String(params.limit));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<ApiResponse<'/v1/admin/evidence/logs/{log_id}/tree-heads', 'get'>>(
            `/v1/admin/evidence/logs/${encodeURIComponent(logId)}/tree-heads${suffix}`,
        );
    }

    async getEvidenceSettings(): Promise<ApiResponse<'/v1/admin/evidence/settings', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/settings', 'get'>>(`/v1/admin/evidence/settings`);
    }

    async putEvidenceSettings<Sent>(
        body: ApiBodyExactly<'/v1/admin/evidence/settings', 'put', Sent>,
    ): Promise<ApiResponse<'/v1/admin/evidence/settings', 'put'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/settings', 'put'>>(`/v1/admin/evidence/settings`, {
            method: 'PUT',
            body: JSON.stringify(body),
        });
    }

    async listEvidenceSigningKeys(): Promise<ApiResponse<'/v1/admin/evidence/keys', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/keys', 'get'>>(`/v1/admin/evidence/keys`);
    }

    /** 409 `per_tenant_key_disabled` while the tenant key is off. */
    async rotateEvidenceKey(): Promise<ApiResponse<'/v1/admin/evidence/keys/rotate', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/evidence/keys/rotate', 'post'>>(`/v1/admin/evidence/keys/rotate`, {
            method: 'POST',
        });
    }

    async buildEvidenceBundle(
        body: ApiRequestBody<'/v1/admin/evidence/bundles', 'post'>,
    ): Promise<import('../evidenceSurfaceTypes').BundleBuildResult> {
        const raw = await this.request<unknown>(`/v1/admin/evidence/bundles`, {
            method: 'POST',
            body: JSON.stringify(body),
            timeout: 150_000,
        });
        return normalizeBundleBuild(raw);
    }

    async verifyEvidenceBundle(
        bundle: Record<string, unknown>,
        bundleDigest?: string | null,
    ): Promise<import('../evidenceSurfaceTypes').BundleVerifyResult> {
        const raw = await this.request<unknown>(`/v1/admin/evidence/bundles/verify`, {
            method: 'POST',
            body: JSON.stringify({ bundle, bundle_digest: bundleDigest ?? null }),
            timeout: 60_000,
        });
        return normalizeBundleVerify(raw);
    }

    /** The blind queue. Pending rows never carry the judged result. */
    async listCheckReviews(
        checkId: string,
        params: { status?: 'pending' | 'graded' | 'skipped'; limit?: number } = {},
    ): Promise<ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews', 'get'>> {
        const qs = new URLSearchParams();
        if (params.status) qs.set('status', params.status);
        if (params.limit !== undefined) qs.set('limit', String(params.limit));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews', 'get'>>(
            `/v1/admin/assurance-checks/${encodeURIComponent(checkId)}/reviews${suffix}`,
        );
    }

    async gradeCheckReview(
        checkId: string,
        reviewId: string,
        body: ApiRequestBody<'/v1/admin/assurance-checks/{check_id}/reviews/{review_id}', 'post'>,
    ): Promise<ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews/{review_id}', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews/{review_id}', 'post'>>(
            `/v1/admin/assurance-checks/${encodeURIComponent(checkId)}/reviews/${encodeURIComponent(reviewId)}`,
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    async drawCheckReviewSample(
        checkId: string,
        week?: string,
    ): Promise<ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews/sample', 'post'>> {
        const suffix = week ? `?week=${encodeURIComponent(week)}` : '';
        return this.request<ApiResponse<'/v1/admin/assurance-checks/{check_id}/reviews/sample', 'post'>>(
            `/v1/admin/assurance-checks/${encodeURIComponent(checkId)}/reviews/sample${suffix}`,
            { method: 'POST', idempotent: true },
        );
    }

    async getCheckAgreement(checkId: string): Promise<ApiResponse<'/v1/admin/assurance-checks/{check_id}/agreement', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/assurance-checks/{check_id}/agreement', 'get'>>(
            `/v1/admin/assurance-checks/${encodeURIComponent(checkId)}/agreement`,
        );
    }

    async listSystemComplianceReports(
        aiSystemId: string,
        params: { framework?: string; limit?: number } = {},
    ): Promise<ApiResponse<'/v1/admin/compliance/reports', 'get'>> {
        const qs = new URLSearchParams({ ai_system_id: aiSystemId });
        if (params.framework) qs.set('framework', params.framework);
        if (params.limit !== undefined) qs.set('limit', String(params.limit));
        return this.request<ApiResponse<'/v1/admin/compliance/reports', 'get'>>(`/v1/admin/compliance/reports?${qs.toString()}`);
    }

    async getEffectiveNoticeConfig(
        aiSystemId: string,
    ): Promise<ApiResponse<'/v1/admin/compliance/notice-configs/effective', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/notice-configs/effective', 'get'>>(
            `/v1/admin/compliance/notice-configs/effective?ai_system_id=${encodeURIComponent(aiSystemId)}`,
        );
    }

    async listSystemDocumentationExports(
        aiSystemId: string,
        framework?: string,
    ): Promise<ApiResponse<'/v1/admin/ai-systems/{ai_system_id}/documentation/exports', 'get'>> {
        const suffix = framework ? `?framework=${encodeURIComponent(framework)}` : '';
        return this.request<ApiResponse<'/v1/admin/ai-systems/{ai_system_id}/documentation/exports', 'get'>>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/documentation/exports${suffix}`,
        );
    }

    /** Renders (and seals, unless unchanged) the documentation skeleton. */
    async exportSystemDocumentation(
        aiSystemId: string,
        framework: string,
    ): Promise<ApiResponse<'/v1/admin/ai-systems/{ai_system_id}/documentation', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/ai-systems/{ai_system_id}/documentation', 'get'>>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/documentation?framework=${encodeURIComponent(framework)}&format=json`,
        );
    }

    /** The printable HTML rendering of the same export. */
    async exportSystemDocumentationHtml(aiSystemId: string, framework: string): Promise<string> {
        return this.request<string>(
            `/v1/admin/ai-systems/${encodeURIComponent(aiSystemId)}/documentation?framework=${encodeURIComponent(framework)}&format=html`,
            { responseType: 'text', headers: { Accept: 'text/html' } },
        );
    }
}
