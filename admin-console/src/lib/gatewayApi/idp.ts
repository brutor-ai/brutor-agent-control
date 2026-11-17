/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Identity providers for single sign-on.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiResponse } from '../api/controlPlaneTypes';
import { GatewayApiBase } from './base';

export class IdpApi extends GatewayApiBase {

    // =============================================================================
    // Identity Provider (IdP) Management Operations (Admin)
    // =============================================================================

    /**
     * List all IdP configurations
     */
    async fetchIdPConfigs(params: {
        enabled_only?: boolean;
    } = {}): Promise<{
        idps: Record<string, any>;
        total: number;
        enabled_count: number;
        accessed_by: any;
    }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.enabled_only) searchParams.append('enabled_only', 'true');

            return await this.request(`/v1/admin/idps?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_idp_configs',
                resource: 'idp_configs',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get specific IdP configuration details
     */
    async fetchIdPConfig(idpId: string): Promise<ApiResponse<'/v1/admin/idps/{idp_id}', 'get'>> {
        try {
            return await this.request(`/v1/admin/idps/${encodeURIComponent(idpId)}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_idp_config',
                resource: 'idp_config',
                idpId
            });
            throw error;
        }
    }

    /**
     * Create new IdP configuration: an OIDC provider, the console's one protocol.
     */
    async createIdPConfig(idpData: {
        name: string;
        issuer: string;
        type: string;
        protocol?: 'oidc';
        well_known_url?: string;
        authorization_endpoint?: string;
        token_endpoint?: string;
        jwks_uri?: string;
        userinfo_endpoint?: string;
        revocation_endpoint?: string;
        introspection_endpoint?: string;
        supports_pkce?: boolean;
        supports_refresh_tokens?: boolean;
        supports_client_credentials?: boolean;
        token_endpoint_auth_methods?: string[];
        scopes_supported?: string[];
        response_types_supported?: string[];
        grant_types_supported?: string[];
        discovery_cache_ttl?: number;
        enabled?: boolean;
        verified?: boolean;
        description?: string;
        logo_url?: string;
        // Accepted `aud` values for workload-identity tokens.
        // Empty: this IdP authenticates no workloads.
        workload_audiences?: string[];
    }): Promise<any> {
        try {
            return await this.request('/v1/admin/idps', {
                method: 'POST',
                body: JSON.stringify(idpData)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_idp_config',
                resource: 'idp_config',
                idpName: idpData.name
            });
            throw error;
        }
    }

    /**
     * Update existing IdP configuration. PATCH semantics — undefined
     * fields are left alone.
     */
    async updateIdPConfig<Sent>(idpId: string, updates: ApiBodyExactly<'/v1/admin/idps/{idp_id}', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/idps/{idp_id}', 'patch'>> {
        try {
            return await this.request(`/v1/admin/idps/${encodeURIComponent(idpId)}`, {
                method: 'PATCH',
                body: JSON.stringify(updates)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_idp_config',
                resource: 'idp_config',
                idpId
            });
            throw error;
        }
    }

    /**
     * Delete IdP configuration
     */
    async deleteIdPConfig(idpId: string): Promise<any> {
        try {
            return await this.request(`/v1/admin/idps/${encodeURIComponent(idpId)}`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_idp_config',
                resource: 'idp_config',
                idpId
            });
            throw error;
        }
    }

    /**
     * Discover IdP endpoints via OIDC discovery
     */
    async discoverIdPEndpoints(idpId: string, forceRefresh: boolean = false): Promise<ApiResponse<'/v1/admin/idps/{idp_id}/discover', 'post'>> {
        try {
            const searchParams = new URLSearchParams();
            if (forceRefresh) searchParams.append('force_refresh', 'true');

            return await this.request(`/v1/admin/idps/${encodeURIComponent(idpId)}/discover?${searchParams}`, {
                method: 'POST'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'discover_idp_endpoints',
                resource: 'idp_discovery',
                idpId
            });
            throw error;
        }
    }

    /**
     * Test existing IdP configuration
     */
    async testExistingIdP(idpId: string): Promise<ApiResponse<'/v1/admin/idps/{idp_id}/test', 'post'>> {
        try {
            return await this.request(`/v1/admin/idps/${encodeURIComponent(idpId)}/test`, {
                method: 'POST'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'test_existing_idp',
                resource: 'idp_test',
                idpId
            });
            throw error;
        }
    }
}
