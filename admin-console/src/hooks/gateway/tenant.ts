/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * The caller's own tenant: the theme the console draws itself in. (A tenant by id — fetchTenant, updateTenant, the theme editor's reads and writes — is Tenant Management: ./multiTenant.ts.)
 *
 * A domain hook of `useGateway`. The facade (../useGateway.ts)
 * calls it with the core and spreads what it returns; call `useGateway`,
 * not this.
 */

import { useCallback } from 'react';
import type { GatewayCore } from './core';


export function useTenantGateway(ctx: Pick<GatewayCore, 'gatewayClient'>) {
    const { gatewayClient } = ctx;

    // The 4-variant theme bundle of the caller's own tenant.
    // TenantThemeContext calls it on auth. (The theme editor reads and
    // writes any tenant's bundle by id on the client: Tenant Management.)
    const fetchCurrentTenantThemeVariants = useCallback(async () => {
        return gatewayClient.fetchCurrentTenantThemeVariants();
    }, [gatewayClient]);

    return {
        fetchCurrentTenantThemeVariants,
    };
}
