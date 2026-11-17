/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * End users.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import { errorKind } from '../../lib/narrow';


export function useEndUsersGateway(ctx: Pick<GatewayCore, 'addError' | 'gatewayClient'>) {
    const { addError, gatewayClient } = ctx;

    // =============================================================================
    // End User Management Operations (Admin)
    // =============================================================================

    const fetchEndUsers = useCallback(async (params: {
        search?: string;
        active_only?: boolean;
        verified_only?: boolean;
        limit?: number;
        offset?: number;
    } = {}) => {
        try {
            return await gatewayClient.fetchEndUsers(params);
        } catch (error) {
            console.error('Failed to fetch end users:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch end users',
                code: 'FETCH_END_USERS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_end_users', resource: 'end_users', ...params }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const fetchEndUserDetails = useCallback(async (userId: number) => {
        try {
            return await gatewayClient.fetchEndUserDetails(userId);
        } catch (error) {
            console.error('Failed to fetch end user details:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to fetch end user details',
                code: 'FETCH_END_USER_DETAILS_ERROR',
                severity: 'medium',
                context: { operation: 'fetch_end_user_details', resource: 'end_user', userId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const updateEndUserStatus = useCallback(async (userId: number, isActive: boolean) => {
        try {
            return await gatewayClient.updateEndUserStatus(userId, isActive);
        } catch (error) {
            console.error('Failed to update end user status:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to update end user status',
                code: 'UPDATE_END_USER_STATUS_ERROR',
                severity: 'medium',
                context: { operation: 'update_end_user_status', resource: 'end_user', userId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const deleteEndUser = useCallback(async (userId: number) => {
        try {
            return await gatewayClient.deleteEndUser(userId);
        } catch (error) {
            console.error('Failed to delete end user:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to delete end user',
                code: 'DELETE_END_USER_ERROR',
                severity: 'medium',
                context: { operation: 'delete_end_user', resource: 'end_user', userId }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    const exportEndUsers = useCallback(async (format: 'csv' | 'json') => {
        try {
            return await gatewayClient.exportEndUsers(format);
        } catch (error) {
            console.error('Failed to export end users:', errorKind(error));
            addError({
                message: error instanceof Error ? error.message : 'Failed to export end users',
                code: 'EXPORT_END_USERS_ERROR',
                severity: 'medium',
                context: { operation: 'export_end_users', resource: 'end_users', format }
            });
            throw error;
        }
    }, [gatewayClient, addError]);

    return {
        deleteEndUser,
        exportEndUsers,
        fetchEndUserDetails,
        fetchEndUsers,
        updateEndUserStatus,
    };
}
