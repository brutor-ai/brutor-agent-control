/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Roles and the administrators of the tenant.
 *
 * A module of the Admin Console's API client. Its methods are
 * installed on `GatewayApiClient` by the module registry (./modules.ts);
 * call them on the client, not on this class.
 */

import type { ApiBodyExactly, ApiQuery, ApiResponse } from '../api/controlPlaneTypes';
import { GatewayApiBase } from './base';

/** A session of a tenant administrator, as the list of sessions answers it. */
export interface TenantAdminSession {
    id: string;
    tenant_id: string;
    admin_id: string;
    admin_username: string | null;
    admin_email: string | null;
    ip_address: string | null;
    user_agent: string | null;
    risk_score: number;
    created_at: string | null;
    last_activity: string | null;
    expires_at: string | null;
    is_active: boolean;
    is_expired: boolean;
    deactivation_reason: string | null;
    /** The session this request was made with. */
    is_current: boolean;
}

/** The sessions of a tenant's administrators. The contract leaves the answer of this route open. */
export interface TenantAdminSessionList {
    sessions: TenantAdminSession[];
    total: number;
    current_session_id: string | null;
}

export class AdminUsersApi extends GatewayApiBase {

    // =============================================================================
    // Role Management Operations
    // =============================================================================

    /**
     * List all roles for current tenant
     */
    async fetchRoles(): Promise<ApiResponse<'/v1/admin/users/roles', 'get'>> {
        try {
            return await this.request('/v1/admin/users/roles');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_roles',
                resource: 'roles'
            });
            throw error;
        }
    }

    /**
     * Get detailed information about a specific role
     */
    async fetchRoleById(roleId: string): Promise<ApiResponse<'/v1/admin/users/roles/{role_id}', 'get'>> {
        try {
            return await this.request(`/v1/admin/users/roles/${roleId}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_role',
                resource: 'role',
                roleId
            });
            throw error;
        }
    }

    /**
     * Create a new custom role
     */
    async createRole(data: {
        name: string;
        description?: string;
        permissions: string[];
    }): Promise<ApiResponse<'/v1/admin/users/roles', 'post'>> {
        try {
            return await this.request('/v1/admin/users/roles', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_role',
                resource: 'role',
                roleName: data.name
            });
            throw error;
        }
    }

    /**
     * Update an existing custom role
     */
    async updateRole<Sent>(roleId: string, data: ApiBodyExactly<'/v1/admin/users/roles/{role_id}', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/users/roles/{role_id}', 'patch'>> {
        try {
            return await this.request(`/v1/admin/users/roles/${roleId}`, {
                method: 'PATCH',
                body: JSON.stringify(data)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_role',
                resource: 'role',
                roleId
            });
            throw error;
        }
    }

    /**
     * Delete a custom role
     */
    async deleteRole(roleId: string): Promise<any> {
        try {
            return await this.request(`/v1/admin/users/roles/${roleId}`, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_role',
                resource: 'role',
                roleId
            });
            throw error;
        }
    }

    /**
     * Assign a role to an admin user
     */
    async assignRoleToAdmin<Sent>(adminId: string, data: ApiBodyExactly<'/v1/admin/users/tenant/{admin_id}/role', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/users/tenant/{admin_id}/role', 'patch'>> {
        try {
            return await this.request(`/v1/admin/users/tenant/${adminId}/role`, {
                method: 'PATCH',
                body: JSON.stringify(data)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'assign_role',
                resource: 'admin',
                adminId,
                roleId: data.role_id
            });
            throw error;
        }
    }

    /**
     * List all available permissions
     */
    async fetchPermissions(): Promise<ApiResponse<'/v1/admin/users/permissions', 'get'>> {
        try {
            return await this.request('/v1/admin/users/permissions');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_permissions',
                resource: 'permissions'
            });
            throw error;
        }
    }

    // =============================================================================
    // Admin User Management Operations
    // =============================================================================

    /**
     * Fetch all tenant admin users
     * Returns admins with role_id and role_name
     */
    async fetchTenantAdminUsers(): Promise<any> {
        try {
            return await this.request('/v1/admin/users/tenant');
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_admin_users',
                resource: 'admins'
            });
            throw error;
        }
    }

    /**
     * Fetch a specific admin user by ID
     */
    async fetchAdminUserById(adminId: string): Promise<ApiResponse<'/v1/admin/users/tenant/{admin_id}', 'get'>> {
        try {
            return await this.request(`/v1/admin/users/tenant/${adminId}`);
        } catch (error) {
            this.errorReporter(error, {
                operation: 'fetch_admin_user',
                resource: 'admin',
                adminId
            });
            throw error;
        }
    }

    /**
     * Create a new tenant admin user
     * role_id is optional - if not provided, defaults to "Tenant Admin" system role
     */
    async createTenantAdminUser(data: {
        tenant_id: string;
        username: string;
        email: string;
        password: string;
        full_name?: string;
        role_id?: string;
    }): Promise<any> {
        try {
            return await this.request('/v1/admin/users/tenant', {
                method: 'POST',
                body: JSON.stringify(data)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'create_admin_user',
                resource: 'admin',
                username: data.username
            });
            throw error;
        }
    }

    /**
     * Update an existing tenant admin user
     * To change role, use assignRoleToAdmin instead
     */
    async updateTenantAdminUser<Sent>(adminId: string, data: ApiBodyExactly<'/v1/admin/users/tenant/{admin_id}', 'patch', Sent>): Promise<ApiResponse<'/v1/admin/users/tenant/{admin_id}', 'patch'>> {
        try {
            return await this.request(`/v1/admin/users/tenant/${adminId}`, {
                method: 'PATCH',
                body: JSON.stringify(data)
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'update_admin_user',
                resource: 'admin',
                adminId
            });
            throw error;
        }
    }

    /**
     * Lift the lock on a tenant administrator's account: the lock, its end
     * and the count of failed sign-ins are cleared. Needs `admin:user:update`;
     * a tenant administrator may unlock administrators of its own tenant.
     * Answers the administrator as it now is.
     */
    async unlockTenantAdminUser(adminId: string): Promise<ApiResponse<'/v1/admin/users/tenant/{admin_id}/unlock', 'post'>> {
        try {
            return await this.request(`/v1/admin/users/tenant/${encodeURIComponent(adminId)}/unlock`, {
                method: 'POST'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'unlock_admin_user',
                resource: 'admin',
                adminId
            });
            throw error;
        }
    }

    /**
     * Delete a tenant admin user
     * @param hardDelete - If true, permanently delete. If false, soft delete (deactivate)
     */
    async deleteTenantAdminUser(adminId: string, hardDelete: boolean = false): Promise<any> {
        try {
            const url = hardDelete
                ? `/v1/admin/users/tenant/${adminId}?hard_delete=true`
                : `/v1/admin/users/tenant/${adminId}`;

            return await this.request(url, {
                method: 'DELETE'
            });
        } catch (error) {
            this.errorReporter(error, {
                operation: 'delete_admin_user',
                resource: 'admin',
                adminId
            });
            throw error;
        }
    }

    /**
     * The sessions of the signed-in tenant administrator's own tenant; the
     * ended ones too when `include_inactive` is set.
     * GET /v1/admin/users/tenant/sessions
     */
    async listTenantAdminSessions(
        query?: Pick<NonNullable<ApiQuery<'/v1/admin/users/tenant/sessions', 'get'>>, 'include_inactive'>,
    ): Promise<TenantAdminSessionList> {
        const qs = query?.include_inactive ? '?include_inactive=true' : '';
        return this.request<TenantAdminSessionList>(`/v1/admin/users/tenant/sessions${qs}`);
    }

    /**
     * End one session of a tenant administrator.
     * POST /v1/admin/users/tenant/sessions/{session_id}/revoke
     */
    async revokeTenantAdminSession(sessionId: string): Promise<unknown> {
        return this.request<unknown>(
            `/v1/admin/users/tenant/sessions/${encodeURIComponent(sessionId)}/revoke`,
            { method: 'POST' },
        );
    }
}
