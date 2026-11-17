/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * End users, end-user groups and user photos.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiRequestBody, ApiResponse } from '../api/controlPlaneTypes';
import { GatewayApiBase } from './base';

/**
 * The answer to the creation of an end user, as far as the console reads it.
 * Written by hand: the contract types the answer as an open record.
 */
export interface CreatedEndUser {
    success?: boolean;
    user?: { id?: number };
    detail?: string;
    [key: string]: unknown;
}

export class EndUsersApi extends GatewayApiBase {

    // =============================================================================
    // End User Management Operations (Admin)
    // =============================================================================

    /**
     * List all end-users for tenant
     */
    async fetchEndUsers(params: {
        search?: string;
        active_only?: boolean;
        verified_only?: boolean;
        limit?: number;
        offset?: number;
    }): Promise<{
        users: any[];
        pagination: {
            total: number;
            offset: number;
            limit: number;
            returned: number;
        };
        filters: any;
    }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.search) searchParams.append('search', params.search);
            if (params.active_only) searchParams.append('active_only', 'true');
            if (params.verified_only) searchParams.append('verified_only', 'true');
            if (params.limit) searchParams.append('limit', params.limit.toString());
            if (params.offset) searchParams.append('offset', params.offset.toString());

            return await this.request(`/v1/admin/end-users?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_end_users',
                resource: 'end_users',
                ...params
            });
            throw error;
        }
    }

    /**
     * Get detailed information about a specific end-user
     */
    async fetchEndUserDetails(userId: number): Promise<ApiResponse<'/v1/admin/end-users/{user_id}', 'get'>> {
        try {
            return await this.request(`/v1/admin/end-users/${encodeURIComponent(userId)}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_end_user_details',
                resource: 'end_user',
                user_id: String(userId)
            });
            throw error;
        }
    }

    /**
     * Update end end-user status (activate/deactivate)
     */
    /**
     * Create an end user. The form that asks shows the refusal itself, so
     * nothing is added to the errors of the page.
     * POST /v1/admin/end-users
     */
    async createEndUser<Sent>(body: ApiBodyExactly<'/v1/admin/end-users', 'post', Sent>): Promise<CreatedEndUser> {
        return this.request<CreatedEndUser>('/v1/admin/end-users', { method: 'POST', body: JSON.stringify(body) });
    }

    async updateEndUserStatus(userId: number, isActive: boolean): Promise<any> {
        try {
            return await this.request(`/v1/admin/end-users/${encodeURIComponent(userId)}/status`, {
                method: 'PATCH',
                body: JSON.stringify({ is_active: isActive } satisfies ApiRequestBody<'/v1/admin/end-users/{user_id}/status', 'patch'>)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_end_user_status',
                resource: 'end_user',
                user_id: String(userId)
            });
            throw error;
        }
    }

    /**
     * Delete an end-user
     */
    async deleteEndUser(userId: number): Promise<any> {
        try {
            return await this.request(`/v1/admin/end-users/${encodeURIComponent(userId)}`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_end_user',
                resource: 'end_user',
                user_id: String(userId)
            });
            throw error;
        }
    }

    /**
     * Export end-users to CSV or JSON
     */
    async exportEndUsers(format: 'csv' | 'json'): Promise<Blob> {
        try {
            return await this.request<Blob>(`/v1/admin/end-users/export/${format}`, {
                method: 'GET'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'export_end_users',
                resource: 'end_users',
                format
            });
            throw error;
        }
    }

    // =============================================================================
    // End User Group Management
    // =============================================================================

    // Named collections of EndUsers, surfaced to operators as a separate
    // construct from Resource Groups. The console authors manual ones; a
    // group an identity provider created is listed by its stored source.
    // Each EndUserGroup can be bound to one or more Resource Groups,
    // granting every current member access to those RGs.

    /**
     * List end-user groups for the current tenant.
     */
    async fetchEndUserGroups(params: {
        search?: string;
        /** Only the groups of this source; the console filters by `manual`. */
        source?: string;
        limit?: number;
        offset?: number;
    } = {}): Promise<{
        groups: any[];
        pagination: { total: number; offset: number; limit: number; returned: number };
        filters: any;
    }> {
        try {
            const searchParams = new URLSearchParams();
            if (params.search) searchParams.append('search', params.search);
            if (params.source) searchParams.append('source', params.source);
            if (params.limit !== undefined) searchParams.append('limit', params.limit.toString());
            if (params.offset !== undefined) searchParams.append('offset', params.offset.toString());
            return await this.request(`/v1/admin/end-user-groups?${searchParams}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_end_user_groups',
                resource: 'end_user_groups',
                ...params
            });
            throw error;
        }
    }

    /**
     * Fetch a single EndUserGroup with its members + bound Resource Groups inlined.
     */
    async fetchEndUserGroupDetail(groupId: string): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}`
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_end_user_group_detail',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    /**
     * Create a manual EndUserGroup.
     */
    async createEndUserGroup(body: {
        name: string;
        display_name?: string;
        description?: string;
        initial_member_ids?: number[];
    }): Promise<any> {
        try {
            return await this.request(`/v1/admin/end-user-groups`, {
                method: 'POST',
                body: JSON.stringify(body),
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_end_user_group',
                resource: 'end_user_group',
                name: body.name,
            });
            throw error;
        }
    }

    /**
     * Partial-update an EndUserGroup. The rename of a group an identity
     * provider created is refused server-side with 409 rename_not_allowed.
     */
    async updateEndUserGroup<Sent>(
        groupId: string,
        body: ApiBodyExactly<'/v1/admin/end-user-groups/{group_id}', 'patch', Sent>
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}`,
                { method: 'PATCH', body: JSON.stringify(body) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_end_user_group',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    async deleteEndUserGroup(groupId: string): Promise<void> {
        try {
            await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}`,
                { method: 'DELETE' }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_end_user_group',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    /** Idempotent add — already-members are skipped. */
    async addEndUserGroupMembers(
        groupId: string,
        endUserIds: number[]
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}/members`,
                { method: 'POST', body: JSON.stringify({ end_user_ids: endUserIds }) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'add_end_user_group_members',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    /** Idempotent remove — non-members are silently skipped. */
    async removeEndUserGroupMembers(
        groupId: string,
        endUserIds: number[]
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}/members`,
                { method: 'DELETE', body: JSON.stringify({ end_user_ids: endUserIds }) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'remove_end_user_group_members',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    /** Authoritative replace of the roster — used by the UI's multi-select save. */
    async setEndUserGroupMembers(
        groupId: string,
        endUserIds: number[]
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}/members`,
                { method: 'PUT', body: JSON.stringify({ end_user_ids: endUserIds } satisfies ApiRequestBody<'/v1/admin/end-user-groups/{group_id}/members', 'put'>) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'set_end_user_group_members',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    /** Bind THIS user-group to one or more Resource Groups. */
    async bindEndUserGroupToResourceGroups(
        groupId: string,
        resourceGroupIds: string[]
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}/resource-groups`,
                { method: 'POST', body: JSON.stringify({ resource_group_ids: resourceGroupIds }) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'bind_end_user_group_to_resource_groups',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    /** Unbind THIS user-group from one or more Resource Groups. */
    async unbindEndUserGroupFromResourceGroups(
        groupId: string,
        resourceGroupIds: string[]
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/end-user-groups/${encodeURIComponent(groupId)}/resource-groups`,
                { method: 'DELETE', body: JSON.stringify({ resource_group_ids: resourceGroupIds }) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'unbind_end_user_group_from_resource_groups',
                resource: 'end_user_group',
                group_id: groupId,
            });
            throw error;
        }
    }

    // RG-side counterparts — used by the Resource Group → Members tab.
    // Symmetric to the UG-side methods above; both hit the same DB
    // table via different URL prefixes.

    async fetchResourceGroupBoundUserGroups(
        resourceGroupId: string
    ): Promise<{ end_user_groups: any[]; total: number }> {
        try {
            return await this.request(
                `/v1/admin/resource-groups/${encodeURIComponent(resourceGroupId)}/end-user-groups`
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_rg_bound_user_groups',
                resource: 'resource_group',
                resource_group_id: resourceGroupId,
            });
            throw error;
        }
    }

    async bindResourceGroupToUserGroups(
        resourceGroupId: string,
        endUserGroupIds: string[]
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/resource-groups/${encodeURIComponent(resourceGroupId)}/end-user-groups`,
                { method: 'POST', body: JSON.stringify({ end_user_group_ids: endUserGroupIds }) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'bind_rg_to_user_groups',
                resource: 'resource_group',
                resource_group_id: resourceGroupId,
            });
            throw error;
        }
    }

    async unbindResourceGroupFromUserGroups(
        resourceGroupId: string,
        endUserGroupIds: string[]
    ): Promise<any> {
        try {
            return await this.request(
                `/v1/admin/resource-groups/${encodeURIComponent(resourceGroupId)}/end-user-groups`,
                { method: 'DELETE', body: JSON.stringify({ end_user_group_ids: endUserGroupIds }) }
            );
        } catch (error) {
            this.errorReporter(error, {
                operation: 'unbind_rg_from_user_groups',
                resource: 'resource_group',
                resource_group_id: resourceGroupId,
            });
            throw error;
        }
    }

    // =========================================================================
    // User Photo Management
    // =========================================================================

    async uploadEndUserPhoto(userId: number, file: File) {
        const formData = new FormData();
        formData.append('file', file);
        return this.request(`/v1/admin/end-users/${userId}/photo`, {
            method: 'POST',
            body: formData,
            headers: {}, // Let browser set Content-Type with boundary
        });
    }

    async deleteEndUserPhoto(userId: number) {
        return this.request(`/v1/admin/end-users/${userId}/photo`, { method: 'DELETE' });
    }

    async uploadAdminPhoto(adminId: string, file: File) {
        const formData = new FormData();
        formData.append('file', file);
        return this.request(`/v1/admin/users/tenant/${adminId}/photo`, {
            method: 'POST',
            body: formData,
            headers: {},
        });
    }

    async deleteAdminPhoto(adminId: string) {
        return this.request(`/v1/admin/users/tenant/${adminId}/photo`, { method: 'DELETE' });
    }
}
