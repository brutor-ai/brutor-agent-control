/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Argument policies: AST-aware checks on tool arguments.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ArgumentPolicy, ArgumentPolicyCreateRequest, SeveritiesBySurface } from '../argumentPolicyTypes';
import { GatewayApiBase } from './base';
import type { ApiBodyExactly, ApiRequestBody } from '../api/controlPlaneTypes';

export class ArgumentPoliciesApi extends GatewayApiBase {

    // ============================================================================
    // Argument Policies (AST-aware argument checks)
    // ============================================================================

    async listArgumentPolicies(params?: { surface?: string; analyzer?: string }) {
        const qs = new URLSearchParams();
        if (params?.surface) qs.set('surface', params.surface);
        if (params?.analyzer) qs.set('analyzer', params.analyzer);
        const suffix = qs.toString() ? `?${qs.toString()}` : '';
        return this.request<{
            policies: ArgumentPolicy[];
            total: number;
            /** The severities a policy and its rules may have on each surface. */
            severities_by_surface?: SeveritiesBySurface;
        }>(`/v1/admin/argument-policies${suffix}`, { method: 'GET' });
    }

    async getArgumentPolicy(id: string) {
        return this.request<ArgumentPolicy>(`/v1/admin/argument-policies/${id}`, {
            method: 'GET',
        });
    }

    async createArgumentPolicy(body: ArgumentPolicyCreateRequest) {
        return this.request<ArgumentPolicy>(`/v1/admin/argument-policies`, {
            method: 'POST',
            body: JSON.stringify(body),
        });
    }

    async updateArgumentPolicy<Sent>(id: string, body: ApiBodyExactly<'/v1/admin/argument-policies/{policy_id}', 'patch', Sent>) {
        return this.request<ArgumentPolicy>(`/v1/admin/argument-policies/${id}`, {
            method: 'PATCH',
            body: JSON.stringify(body),
        });
    }

    async deleteArgumentPolicy(id: string) {
        return this.request<void>(`/v1/admin/argument-policies/${id}`, {
            method: 'DELETE',
        });
    }

    async setArgumentPolicyGroups(id: string, group_ids: string[]) {
        return this.request<ArgumentPolicy>(`/v1/admin/argument-policies/${id}/groups`, {
            method: 'PATCH',
            body: JSON.stringify({ group_ids } satisfies ApiRequestBody<'/v1/admin/argument-policies/{policy_id}/groups', 'patch'>),
        });
    }

    async exportArgumentPoliciesYaml(): Promise<string> {
        return this.request<string>('/v1/admin/argument-policies/export.yaml', {
            method: 'GET',
            headers: { Accept: 'application/x-yaml' },
            responseType: 'text',
        });
    }

    async importArgumentPoliciesYaml(yaml: string) {
        return this.request<{
            created: string[];
            updated: string[];
            unchanged: string[];
            errors: { index: number; name?: string; error: string }[];
            unresolved_groups: string[];
        }>('/v1/admin/argument-policies/import.yaml', {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-yaml', Accept: 'application/json' },
            body: yaml,
            responseType: 'json',
        });
    }
}
