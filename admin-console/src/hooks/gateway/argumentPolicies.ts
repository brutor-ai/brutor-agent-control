/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Argument policies.
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';
import type { ApiBodyExactly } from '../../lib/api/controlPlaneTypes';


export function useArgumentPoliciesGateway(ctx: Pick<GatewayCore, 'gatewayClient'>) {
    const { gatewayClient } = ctx;

    // ============================================
    // Argument Policies (AST checks)
    // ============================================

    const listArgumentPolicies = useCallback(async (
        params?: { surface?: string; analyzer?: string },
    ) => gatewayClient.listArgumentPolicies(params), [gatewayClient]);

    const getArgumentPolicy = useCallback(async (id: string) =>
        gatewayClient.getArgumentPolicy(id), [gatewayClient]);

    const createArgumentPolicy = useCallback(async (
        body: import('../../lib/argumentPolicyTypes').ArgumentPolicyCreateRequest,
    ) => gatewayClient.createArgumentPolicy(body), [gatewayClient]);

    const updateArgumentPolicy = useCallback(async <Sent>(
        id: string,
        body: ApiBodyExactly<'/v1/admin/argument-policies/{policy_id}', 'patch', Sent>,
    ) => gatewayClient.updateArgumentPolicy(id, body), [gatewayClient]);

    const deleteArgumentPolicy = useCallback(async (id: string) =>
        gatewayClient.deleteArgumentPolicy(id), [gatewayClient]);

    const setArgumentPolicyGroups = useCallback(async (
        id: string,
        group_ids: string[],
    ) => gatewayClient.setArgumentPolicyGroups(id, group_ids), [gatewayClient]);

    const exportArgumentPoliciesYaml = useCallback(async () =>
        gatewayClient.exportArgumentPoliciesYaml(), [gatewayClient]);

    const importArgumentPoliciesYaml = useCallback(async (yaml: string) =>
        gatewayClient.importArgumentPoliciesYaml(yaml), [gatewayClient]);

    return {
        createArgumentPolicy,
        deleteArgumentPolicy,
        exportArgumentPoliciesYaml,
        getArgumentPolicy,
        importArgumentPoliciesYaml,
        listArgumentPolicies,
        setArgumentPolicyGroups,
        updateArgumentPolicy,
    };
}
