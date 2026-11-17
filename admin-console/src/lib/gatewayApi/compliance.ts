/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The compliance console, ISO 42001 coverage and the framework exports.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';
import { normalizeVerifyResult } from '../complianceEvidenceTypes';
import type {
    AIAsset,
    AIAssetSyncResponse,
} from '../aiAssetTypes';
import type { Factsheet, FactsheetSnapshot, FactsheetSnapshotListResponse } from '../factsheetTypes';
import { GatewayApiBase } from './base';

export class ComplianceApi extends GatewayApiBase {

    // =========================================================================
    // Compliance console (src/components/compliance/**): overview,
    // obligations matrix + drill-in, profiles table, oversight, transparency
    // and notice configs, Evidence Reports, the incident register, and inline
    // record verification. Bundle build / verify and documentation exports
    // are shared with the evidence surfaces above.
    // =========================================================================

    private complianceQuery(params: Record<string, string | number | boolean | null | undefined>): string {
        const qs = new URLSearchParams();
        for (const [key, value] of Object.entries(params)) {
            if (value !== undefined && value !== null && value !== '') qs.set(key, String(value));
        }
        const s = qs.toString();
        return s ? `?${s}` : '';
    }

    async getComplianceOverview(
        framework: string,
        window: { from?: string; to?: string } = {},
    ): Promise<ApiResponse<'/v1/admin/compliance/overview', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/overview', 'get'>>(
            `/v1/admin/compliance/overview${this.complianceQuery({ framework, ...window })}`,
        );
    }

    /** The board plus the system × obligation matrix (`include_matrix`). */
    async getComplianceObligations(params: {
        framework: string;
        include_matrix?: boolean;
        ai_system_id?: string;
        from?: string;
        to?: string;
    }): Promise<ApiResponse<'/v1/admin/compliance/obligations', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/obligations', 'get'>>(
            `/v1/admin/compliance/obligations${this.complianceQuery(params)}`,
        );
    }

    async getComplianceObligationDetail(
        key: string,
        params: { framework: string; ai_system_id?: string; from?: string; to?: string },
    ): Promise<ApiResponse<'/v1/admin/compliance/obligations/{key}', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/obligations/{key}', 'get'>>(
            `/v1/admin/compliance/obligations/${encodeURIComponent(key)}${this.complianceQuery(params)}`,
        );
    }

    async listComplianceSystems(): Promise<ApiResponse<'/v1/admin/compliance/systems', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/systems', 'get'>>(`/v1/admin/compliance/systems`);
    }

    async getComplianceOversight(
        params: { ai_system_id?: string; from?: string; to?: string } = {},
    ): Promise<ApiResponse<'/v1/admin/compliance/oversight', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/oversight', 'get'>>(
            `/v1/admin/compliance/oversight${this.complianceQuery(params)}`,
        );
    }

    async getComplianceTransparency(
        params: { ai_system_id?: string; from?: string; to?: string } = {},
    ): Promise<ApiResponse<'/v1/admin/compliance/transparency', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/transparency', 'get'>>(
            `/v1/admin/compliance/transparency${this.complianceQuery(params)}`,
        );
    }

    async listNoticeConfigs(): Promise<ApiResponse<'/v1/admin/compliance/notice-configs', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/notice-configs', 'get'>>(
            `/v1/admin/compliance/notice-configs`,
        );
    }

    /** Save the tenant default (no `aiSystemId`) or one system's config. */
    async putNoticeConfig<Sent>(
        body: ApiBodyExactly<'/v1/admin/compliance/notice-configs', 'put', Sent>,
        aiSystemId?: string,
    ): Promise<ApiResponse<'/v1/admin/compliance/notice-configs', 'put'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/notice-configs', 'put'>>(
            `/v1/admin/compliance/notice-configs${this.complianceQuery({ ai_system_id: aiSystemId })}`,
            { method: 'PUT', body: JSON.stringify(body) },
        );
    }

    async deleteNoticeConfig(
        configId: string,
    ): Promise<ApiResponse<'/v1/admin/compliance/notice-configs/{config_id}', 'delete'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/notice-configs/{config_id}', 'delete'>>(
            `/v1/admin/compliance/notice-configs/${encodeURIComponent(configId)}`,
            { method: 'DELETE' },
        );
    }

    /** `ai_system_id: 'estate'` lists estate-wide reports only. */
    async listComplianceReports(
        params: { framework?: string; ai_system_id?: string; cadence?: string; limit?: number } = {},
    ): Promise<ApiResponse<'/v1/admin/compliance/reports', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/reports', 'get'>>(
            `/v1/admin/compliance/reports${this.complianceQuery(params)}`,
        );
    }

    async getComplianceReport(reportId: string): Promise<ApiResponse<'/v1/admin/compliance/reports/{report_id}', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/reports/{report_id}', 'get'>>(
            `/v1/admin/compliance/reports/${encodeURIComponent(reportId)}`,
        );
    }

    /** Idempotent per period; `force` regenerates in place. */
    async generateComplianceReport(
        body: ApiRequestBody<'/v1/admin/compliance/reports', 'post'>,
    ): Promise<ApiResponse<'/v1/admin/compliance/reports', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/reports', 'post'>>(`/v1/admin/compliance/reports`, {
            method: 'POST',
            body: JSON.stringify(body),
            timeout: 150_000,
        });
    }

    /** The report's evidence bundle and the digest the CP states for it. */
    async getComplianceReportBundle(reportId: string): Promise<{ bundle: unknown; digest: string | null }> {
        const response = await this.request<Response>(
            `/v1/admin/compliance/reports/${encodeURIComponent(reportId)}/bundle`,
            { responseType: 'raw' },
        );
        const bundle: unknown = await response.json();
        return { bundle, digest: response.headers.get('X-Brutor-Bundle-Digest') };
    }

    async listComplianceIncidents(
        params: { status?: string; framework?: string; ai_system_id?: string } = {},
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents', 'get'>>(
            `/v1/admin/compliance/incidents${this.complianceQuery(params)}`,
        );
    }

    async getComplianceIncident(
        incidentId: string,
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}', 'get'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}', 'get'>>(
            `/v1/admin/compliance/incidents/${encodeURIComponent(incidentId)}`,
        );
    }

    async createComplianceIncident(
        body: ApiRequestBody<'/v1/admin/compliance/incidents', 'post'>,
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents', 'post'>>(`/v1/admin/compliance/incidents`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    async updateComplianceIncident<Sent>(
        incidentId: string,
        body: ApiBodyExactly<'/v1/admin/compliance/incidents/{incident_id}', 'patch', Sent>,
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}', 'patch'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}', 'patch'>>(
            `/v1/admin/compliance/incidents/${encodeURIComponent(incidentId)}`,
            { method: 'PATCH', body: JSON.stringify(body) },
        );
    }

    async reportComplianceIncident(
        incidentId: string,
        body: ApiRequestBody<'/v1/admin/compliance/incidents/{incident_id}/report', 'post'>,
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/report', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/report', 'post'>>(
            `/v1/admin/compliance/incidents/${encodeURIComponent(incidentId)}/report`,
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    /** Record the submission of one stage of a staged report (DORA, NIS2). */
    async recordComplianceIncidentSubmission(
        incidentId: string,
        body: ApiRequestBody<'/v1/admin/compliance/incidents/{incident_id}/submissions', 'post'>,
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/submissions', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/submissions', 'post'>>(
            `/v1/admin/compliance/incidents/${encodeURIComponent(incidentId)}/submissions`,
            { method: 'POST', body: JSON.stringify(body) },
        );
    }

    async closeComplianceIncident(
        incidentId: string,
        note?: string,
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/close', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/close', 'post'>>(
            `/v1/admin/compliance/incidents/${encodeURIComponent(incidentId)}/close`,
            { method: 'POST', body: JSON.stringify({ note: note || null }) },
        );
    }

    async addComplianceIncidentNote(
        incidentId: string,
        note: string,
    ): Promise<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/events', 'post'>> {
        return this.request<ApiResponse<'/v1/admin/compliance/incidents/{incident_id}/events', 'post'>>(
            `/v1/admin/compliance/incidents/${encodeURIComponent(incidentId)}/events`,
            { method: 'POST', body: JSON.stringify({ note }) },
        );
    }

    /** Verify a record supplied inline (a record JSON and its Signed
     *  Statement) — the statement check runs inside the record verifier. */
    async verifyEvidenceInline(
        record: Record<string, unknown>,
        statementB64: string | null,
    ): Promise<import('../complianceEvidenceTypes').EvidenceVerifyResult> {
        const raw = await this.request<unknown>(`/v1/admin/evidence/verify`, {
            method: 'POST',
            body: JSON.stringify({ record, statement_b64: statementB64 }),
        });
        return normalizeVerifyResult(raw);
    }

    async updateAIAsset<Sent>(
        id: string,
        body: ApiBodyExactly<'/v1/admin/assets/{asset_id}', 'patch', Sent>,
    ) {
        return this.request<AIAsset>(`/v1/admin/assets/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(body),
        });
    }

    async deleteAIAsset(id: string): Promise<void> {
        return this.request<void>(`/v1/admin/assets/${id}`, {
            method: 'DELETE',
        });
    }

    async syncAIAssets(): Promise<AIAssetSyncResponse> {
        return this.request<AIAssetSyncResponse>(`/v1/admin/assets/sync`, {
            method: 'POST',
        });
    }

    // ---- Asset Factsheet (per-asset governance drill-down) ----

    /**
     * Fetch the live factsheet roll-up for an asset over a window.
     * Tenant comes from the JWT — no tenant_id passed.
     */
    async getFactsheet(
        assetId: string,
        params?: { period_hours?: number },
    ): Promise<Factsheet> {
        const qs = new URLSearchParams();
        if (params?.period_hours !== undefined) {
            qs.set('period_hours', String(params.period_hours));
        }
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<Factsheet>(
            `/v1/admin/assets/${assetId}/factsheet${suffix}`,
            { method: 'GET' },
        );
    }

    /**
     * Stream the factsheet as a CSV blob. The base `request` returns a
     * Blob for `text/csv` responses (see content-type handling).
     */
    async exportFactsheetCsv(
        assetId: string,
        params?: { period_hours?: number },
    ): Promise<Blob> {
        const qs = new URLSearchParams();
        qs.set('format', 'csv');
        if (params?.period_hours !== undefined) {
            qs.set('period_hours', String(params.period_hours));
        }
        return this.request<Blob>(
            `/v1/admin/assets/${assetId}/factsheet?${qs.toString()}`,
            { method: 'GET', headers: { Accept: 'text/csv' } },
        );
    }

    /** Capture an immutable factsheet snapshot for audit evidence. */
    async captureFactsheetSnapshot(
        assetId: string,
        period_hours?: number,
    ): Promise<FactsheetSnapshot> {
        const qs = new URLSearchParams();
        if (period_hours !== undefined) {
            qs.set('period_hours', String(period_hours));
        }
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<FactsheetSnapshot>(
            `/v1/admin/assets/${assetId}/factsheet/snapshot${suffix}`,
            { method: 'POST' },
        );
    }

    async listFactsheetSnapshots(
        assetId: string,
        params?: { limit?: number; offset?: number },
    ): Promise<FactsheetSnapshotListResponse> {
        const qs = new URLSearchParams();
        if (params?.limit !== undefined) qs.set('limit', String(params.limit));
        if (params?.offset !== undefined) qs.set('offset', String(params.offset));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<FactsheetSnapshotListResponse>(
            `/v1/admin/assets/${assetId}/factsheet/snapshots${suffix}`,
            { method: 'GET' },
        );
    }

    async getFactsheetSnapshot(
        assetId: string,
        snapshotId: string,
    ): Promise<FactsheetSnapshot> {
        return this.request<FactsheetSnapshot>(
            `/v1/admin/assets/${assetId}/factsheet/snapshots/${snapshotId}`,
            { method: 'GET' },
        );
    }

    // ============================================================================
    // Compliance (framework mapping + exports)
    // ============================================================================

    async getComplianceProfile() {
        return this.request<{
            tenant_id: string;
            profile: { frameworks: string[] };
            known_frameworks: string[];
        }>(`/v1/admin/compliance/profile`, { method: 'GET' });
    }

    async getComplianceDashboard(params?: { from?: string; to?: string }) {
        const qs = new URLSearchParams();
        if (params?.from) qs.set('from', params.from);
        if (params?.to) qs.set('to', params.to);
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<{
            tenant_id: string;
            from: string;
            to: string;
            cards: Array<{
                framework: string;
                declared: boolean;
                total_tagged_rows: number;
                by_tag: Record<string, number>;
            }>;
        }>(`/v1/admin/compliance/dashboard${suffix}`, { method: 'GET' });
    }

    /**
     * Export endpoint — supports both JSON (default) and CSV.
     * CSV requests stream directly to the browser via a fresh
     * fetch so we don't buffer multi-MB exports in memory.
     */
    private buildComplianceExportUrl(
        path: string,
        params?: { from?: string; to?: string; format?: 'json' | 'csv' },
    ): string {
        const qs = new URLSearchParams();
        if (params?.from) qs.set('from', params.from);
        if (params?.to) qs.set('to', params.to);
        if (params?.format) qs.set('format', params.format);
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return `/v1/admin/compliance/${path}${suffix}`;
    }

    async getGdprArticle30(params?: { from?: string; to?: string }) {
        const path = this.buildComplianceExportUrl('gdpr/article-30', { ...params, format: 'json' });
        return this.request<{
            tenant_id: string;
            from: string;
            to: string;
            count: number;
            records: Array<{
                id: string;
                timestamp: string;
                proxy_type: string;
                proxy_subtype: string | null;
                gateway_user_id: string | null;
                idp_user_id: string | null;
                idp_user_email: string | null;
            }>;
        }>(path, { method: 'GET' });
    }

    async getEuAiActRiskClassification(params?: { from?: string; to?: string }) {
        const path = this.buildComplianceExportUrl('eu-ai-act/risk-classification', params);
        return this.request<{
            tenant_id: string;
            from: string;
            to: string;
            calls_by_tier: Record<string, number>;
            rg_classifications: Array<{ id: string; name: string; tier: string }>;
        }>(path, { method: 'GET' });
    }

    async getSoc2ControlCoverage(params?: { from?: string; to?: string; sample_size?: number }) {
        const qs = new URLSearchParams();
        if (params?.from) qs.set('from', params.from);
        if (params?.to) qs.set('to', params.to);
        if (params?.sample_size != null) qs.set('sample_size', String(params.sample_size));
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<{
            tenant_id: string;
            from: string;
            to: string;
            controls: Array<{
                control: string;
                evidence_count: number;
                sample_row_ids: string[];
            }>;
        }>(`/v1/admin/compliance/soc2/control-coverage${suffix}`, { method: 'GET' });
    }

    async getHipaaPhiAccess(params?: { from?: string; to?: string }) {
        const path = this.buildComplianceExportUrl('hipaa/phi-access', { ...params, format: 'json' });
        return this.request<{
            tenant_id: string;
            from: string;
            to: string;
            count: number;
            records: Array<{
                id: string;
                timestamp: string;
                proxy_type: string;
                gateway_user_id: string | null;
                idp_user_id: string | null;
                idp_user_email: string | null;
                resource_group_id: string | null;
                source: 'detected' | 'rg_marked';
            }>;
        }>(path, { method: 'GET' });
    }

    async getIso42001Activity(params?: { from?: string; to?: string }) {
        const path = this.buildComplianceExportUrl('iso-42001/activity', params);
        return this.request<{
            tenant_id: string;
            from: string;
            to: string;
            total_activity_rows: number;
            by_proxy_type: Record<string, number>;
        }>(path, { method: 'GET' });
    }

    /**
     * Download a regime export as CSV. Streams the blob and
     * triggers a save-as via a synthetic anchor — same pattern
     * as the audit-log CSV export.
     */
    async downloadComplianceExportCsv(
        regime: 'gdpr/article-30' | 'hipaa/phi-access',
        params?: { from?: string; to?: string },
    ): Promise<Blob> {
        const path = this.buildComplianceExportUrl(regime, { ...params, format: 'csv' });
        // Tenant context is carried on the JWT — no explicit
        // X-Tenant-ID header needed for admin-scoped fetches.
        return this.request<Blob>(path, {
            method: 'GET',
            headers: { Accept: 'text/csv' },
            responseType: 'blob',
        });
    }
}
