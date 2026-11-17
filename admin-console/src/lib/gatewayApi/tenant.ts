/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The caller's own tenant: the theme the console draws itself in, and the search-provider configuration. (Its record and settings are read and saved at `/v1/admin/tenants/current` by the Settings → Tenant screen. A tenant by id is tenant management: ./multiTenant.ts.)
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiResponse } from '../api/controlPlaneTypes';
import { TenantThemeVariantsBundle } from '../../context/TenantThemeContext';
import { GatewayApiBase } from './base';

/**
 * The caller's own tenant, as far as the console reads it. Written by hand:
 * the Control Plane serves `GET /v1/admin/tenants/current` without a
 * response model, so the contract types its answer `unknown`. A field
 * beside these is read with a narrowing helper (src/lib/narrow.ts).
 */
/**
 * What `/v1/admin/tenants/current` answers: the administrator's own tenant,
 * or, to a system administrator, who belongs to no tenant, `NoCurrentTenant`
 * (`id` null, the three objects empty).
 */
export type CurrentTenantAnswer = ApiResponse<'/v1/admin/tenants/current', 'get'>;

/** The administrator's own tenant. */
export type CurrentTenant = Extract<CurrentTenantAnswer, { id: string }>;

/** The tenant an answer of `/v1/admin/tenants/current` is, or null for an administrator of no tenant. */
export function ownTenantOf(answer: CurrentTenantAnswer): CurrentTenant | null {
    return answer.id === null ? null : answer;
}

export class TenantApi extends GatewayApiBase {

    /**
     * The record of the administrator's own tenant.
     * GET /v1/admin/tenants/current
     */
    async getCurrentTenant(): Promise<CurrentTenantAnswer> {
        return this.request<CurrentTenantAnswer>('/v1/admin/tenants/current');
    }

    /**
     * Update the settings of the administrator's own tenant.
     * PATCH /v1/admin/tenants/current
     */
    async updateCurrentTenantSettings<Sent>(
        settings: ApiBodyExactly<'/v1/admin/tenants/current', 'patch', Sent>,
    ): Promise<ApiResponse<'/v1/admin/tenants/current', 'patch'>> {
        return this.request('/v1/admin/tenants/current', {
            method: 'PATCH',
            body: JSON.stringify(settings),
        });
    }

    /**
     * Fetch the 4-variant theme bundle of the caller's own tenant: the theme
     * the console draws itself in.
     * GET /v1/admin/tenants/current/theme-variants
     */
    async fetchCurrentTenantThemeVariants(): Promise<TenantThemeVariantsBundle> {
        try {
            return await this.request<TenantThemeVariantsBundle>('/v1/admin/tenants/current/theme-variants');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_current_tenant_theme_variants',
                resource: 'tenant_theme_variants',
            });
            throw error;
        }
    }

    // =========================================================================
    // System Servers — Search Configuration
    // =========================================================================

    async getSearchProviderConfig(): Promise<ApiResponse<'/v1/admin/system-servers/search-config', 'get'>> {
        return this.request('/v1/admin/system-servers/search-config');
    }

    async updateSearchProviderConfig<Sent>(config: ApiBodyExactly<'/v1/admin/system-servers/search-config', 'put', Sent>): Promise<ApiResponse<'/v1/admin/system-servers/search-config', 'put'>> {
        return this.request('/v1/admin/system-servers/search-config', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(config),
        });
    }

    async clearSearchApiKey(): Promise<ApiResponse<'/v1/admin/system-servers/search-config/api-key', 'delete'>> {
        return this.request('/v1/admin/system-servers/search-config/api-key', {
            method: 'DELETE',
        });
    }
}
