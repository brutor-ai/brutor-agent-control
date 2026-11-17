/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * OAuth tokens held for MCP servers and the OAuth app clients.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiResponse } from '../api/controlPlaneTypes';
import {
    OAuthTokensListResponse,
    OAuthTokenStatus,
    OAuthTokensRevokeResponse,
    OAuthTokensBulkRevokeRequest,
} from '../groupTypes';
import { GatewayApiBase } from './base';

export class OAuthApi extends GatewayApiBase {

    // =============================================================================
    // OAuth Tokens (admin read-only diagnostic view). Encrypted token
    // material is never returned by the server — the response contains
    // `has_refresh_token: boolean` instead of the raw refresh token.
    // =============================================================================

    /**
     * List OAuth tokens for the current tenant with per-row diagnostic
     * status ("active", "expired_with_refresh", "expired_no_refresh",
     * "revoked", "idp_unavailable"). Use to triage "why did this MCP
     * call 403?" from the admin console.
     *
     * GET /v1/admin/oauth-tokens
     */
    async fetchOAuthTokens(params?: {
        mcp_server_id?: string;
        idp_id?: string;
        gateway_user_id?: string;
        status?: OAuthTokenStatus;
        limit?: number;
    }): Promise<OAuthTokensListResponse> {
        const qs = new URLSearchParams();
        if (params?.mcp_server_id) qs.append('mcp_server_id', params.mcp_server_id);
        if (params?.idp_id) qs.append('idp_id', params.idp_id);
        if (params?.gateway_user_id) qs.append('gateway_user_id', params.gateway_user_id);
        if (params?.status) qs.append('status', params.status);
        if (params?.limit) qs.append('limit', String(params.limit));
        const q = qs.toString();
        return this.request<OAuthTokensListResponse>(
            `/v1/admin/oauth-tokens${q ? `?${q}` : ''}`,
        );
    }

    /**
     * Revoke a single OAuth token. Marks the row revoked, destroys both
     * encrypted access + refresh tokens in our DB, and best-effort
     * calls the IdP's RFC 7009 revocation_endpoint to invalidate
     * copies of the token at the source. The user will see a
     * "Re-authorize" banner on their next MCP call.
     *
     * POST /v1/admin/oauth-tokens/{tokenId}/revoke
     */
    async revokeOAuthToken(tokenId: number): Promise<OAuthTokensRevokeResponse> {
        return this.request<OAuthTokensRevokeResponse>(
            `/v1/admin/oauth-tokens/${tokenId}/revoke`,
            { method: 'POST' },
        );
    }

    /**
     * Revoke every OAuth token matching the predicate. Same filter
     * keys as `fetchOAuthTokens`. An empty body revokes every
     * non-revoked token in the tenant — the UI must confirm before
     * sending that.
     *
     * POST /v1/admin/oauth-tokens/revoke-bulk
     */
    async revokeOAuthTokensBulk(
        filter: OAuthTokensBulkRevokeRequest,
    ): Promise<OAuthTokensRevokeResponse> {
        return this.request<OAuthTokensRevokeResponse>(
            `/v1/admin/oauth-tokens/revoke-bulk`,
            {
                method: 'POST',
                body: JSON.stringify(filter),
            },
        );
    }

    // =============================================================================
    // OAuth App Client Management Operations (Admin)
    // =============================================================================

    /**
     * List all OAuth app clients
     */
    async fetchOAuthAppClients(params: {
        app_type?: string;
        idp_id?: string;
        active_only?: boolean;
    } = {}): Promise<{ clients: any[]; total: number }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.app_type) searchParams.append('app_type', params.app_type);
            if (params.idp_id) searchParams.append('idp_id', params.idp_id);
            if (params.active_only) searchParams.append('active_only', 'true');

            return await this.request(`/v1/admin/oauth-app-clients?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_oauth_app_clients',
                resource: 'oauth_app_clients',
                ...params
            });
            throw error;
        }
    }

    /**
     * Create a new OAuth app client
     */
    async createOAuthAppClient(clientData: {
        name: string;
        app_type: 'webapp' | 'mobile' | 'desktop' | 'cli' | 'service';
        idp_id: string;
        client_id: string;
        client_secret?: string;
        redirect_uri: string;
        scopes: string[];
        client_type: 'confidential' | 'public';
        supports_pkce?: boolean;
        allowed_grant_types?: string[];
        description?: string;
    }): Promise<any> {
        try {
            return await this.request('/v1/admin/oauth-app-clients', {
                method: 'POST',
                body: JSON.stringify(clientData)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_oauth_app_client',
                resource: 'oauth_app_client',
                client_name: clientData.name
            });
            throw error;
        }
    }

    /**
     * Update an existing OAuth app client
     */
    async updateOAuthAppClient<Sent>(clientId: string, updates: ApiBodyExactly<'/v1/admin/oauth-app-clients/{client_id}', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/oauth-app-clients/{client_id}', 'patch'>> {
        try {
            return await this.request(`/v1/admin/oauth-app-clients/${encodeURIComponent(clientId)}`, {
                method: 'PATCH',
                body: JSON.stringify(updates)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_oauth_app_client',
                resource: 'oauth_app_client',
                clientId
            });
            throw error;
        }
    }

    /**
     * Delete an OAuth app client
     */
    async deleteOAuthAppClient(clientId: string): Promise<any> {
        try {
            return await this.request(`/v1/admin/oauth-app-clients/${encodeURIComponent(clientId)}`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_oauth_app_client',
                resource: 'oauth_app_client',
                clientId
            });
            throw error;
        }
    }

    /**
     * Get usage information for an OAuth app client
     */
    async getOAuthAppClientUsage(clientId: string): Promise<any> {
        try {
            return await this.request(`/v1/admin/oauth-app-clients/${encodeURIComponent(clientId)}/usage`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'get_oauth_app_client_usage',
                resource: 'oauth_app_client',
                clientId
            });
            throw error;
        }
    }
}
