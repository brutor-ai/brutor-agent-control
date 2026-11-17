/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The administrators of the tenant and their roles.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useState, useCallback } from 'react';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';
import { errorKind } from '../../lib/narrow';

interface AdminUser {
    id: string;
    username: string;
    email: string;
    full_name?: string;
    roles: string[];
    is_active: boolean;
}
export function useAdminUsersGateway(ctx: Pick<GatewayCore, 'addError' | 'enableAuth' | 'gatewayClient' | 'isAuthenticated'>) {
    const { addError, enableAuth, gatewayClient, isAuthenticated } = ctx;

    // User management state
    const [users, setUsers] = useState<AdminUser[]>([]);
    const [usersLoaded, setUsersLoaded] = useState(false);
    const [loadingUsers, setLoadingUsers] = useState(false);

    // =============================================================================
    // Data Fetching Functions
    // =============================================================================

    const fetchUsers = useCallback(async (): Promise<AdminUser[]> => {
        if (!enableAuth || !isAuthenticated) {
            return [];
        }

        setLoadingUsers(true);
        try {
            const response = await gatewayClient.fetchTenantAdminUsers();
            // GET /v1/admin/users/tenant answers `{ admins: [...] }`.
            const userData = response.admins || response.users || [];
            setUsers(userData);
            setUsersLoaded(true);
            return userData;
        } catch (error) {
            console.error('Failed to load users:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to load users',
                code: 'FETCH_USERS_ERROR',
                severity: 'low',
                context: { operation: 'fetch_users', resource: 'users' }
            });
            setUsersLoaded(true);
            return [];
        } finally {
            setLoadingUsers(false);
        }
    }, [gatewayClient, enableAuth, isAuthenticated, addError]);

    // Helper functions for users
    const getUsernameById = useCallback((userId: string): string => {
        const user = users.find(u => u.id === userId);
        return user ? user.username : userId;
    }, [users]);

    const getUserDisplayName = useCallback((userId: string): string => {
        const user = users.find(u => u.id === userId);
        return user ? (user.full_name || user.username) : userId;
    }, [users]);

    const getUserRoles = useCallback((userId: string): string[] => {
        const user = users.find(u => u.id === userId);
        return user ? user.roles : [];
    }, [users]);

    // =============================================================================
    // Admin User Management Operations
    // =============================================================================

    const createUser = useCallback(async (userData: {
        tenant_id: string;
        username: string;
        email: string;
        password: string;
        full_name?: string | undefined;
        role_id?: string | undefined;
    }) => {
        try {
            const result = await gatewayClient.createTenantAdminUser(userData);
            await fetchUsers();
            return result;
        } catch (error) {
            console.error('Failed to create user:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to create user',
                code: 'CREATE_USER_ERROR',
                severity: 'medium',
                context: { operation: 'create_user', resource: 'user', username: userData.username }
            });
            throw error;
        }
    }, [gatewayClient, fetchUsers, addError]);

    const updateUser = useCallback(async <Sent>(userId: string, userData: ApiBodyExactly<'/v1/admin/users/tenant/{admin_id}', 'patch', Sent>) => {
        try {
            const result = await gatewayClient.updateTenantAdminUser(userId, userData);
            await fetchUsers();
            return result;
        } catch (error) {
            console.error('Failed to update user:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to update user',
                code: 'UPDATE_USER_ERROR',
                severity: 'medium',
                context: { operation: 'update_user', resource: 'user', userId }
            });
            throw error;
        }
    }, [gatewayClient, fetchUsers, addError]);

    const deleteUser = useCallback(async (userId: string) => {
        try {
            const result = await gatewayClient.deleteTenantAdminUser(userId);
            await fetchUsers();
            return result;
        } catch (error) {
            console.error('Failed to delete user:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete user',
                code: 'DELETE_USER_ERROR',
                severity: 'medium',
                context: { operation: 'delete_user', resource: 'user', userId }
            });
            throw error;
        }
    }, [gatewayClient, fetchUsers, addError]);

    const fetchRoles = useCallback(async () => {
        try {
            return await gatewayClient.fetchRoles();
        } catch (error) {
            console.error('Failed to fetch roles:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch roles',
                code: 'FETCH_ROLES_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_roles', resource: 'roles' }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        createUser,
        deleteUser,
        fetchRoles,
        fetchUsers,
        getUserDisplayName,
        getUserRoles,
        getUsernameById,
        loadingUsers,
        updateUser,
        users,
        usersLoaded,
    };
}
