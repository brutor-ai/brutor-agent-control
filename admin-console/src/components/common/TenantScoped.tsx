/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * A page that shows one tenant's data.
 *
 * A tenant administrator is of one tenant: the page is shown as it is. An
 * administrator of no tenant (a contributed principal, slot
 * `app.principals`: the system administrator) is answered 400 by the
 * control plane on every tenant-scoped route unless the request names a
 * tenant. For such an administrator the page asks which tenant to show,
 * with the choice its principal contributes (`TenantChoice`), in place of
 * the page; then the API client names the chosen tenant on its requests
 * (`setActingTenant`) for as long as the page is shown.
 *
 * The choice is remembered for the next such page of the same visit.
 */

import React, { useEffect, useState } from 'react';
import { Building2 } from 'lucide-react';

import { useAuth } from '../../hooks/useAuth';
import { setActingTenant } from '../../lib/gatewayApi/base';
import { chosenTenant, rememberChosenTenant } from './chosenTenant';

export const TenantScoped: React.FC<{ pageName: string; children: React.ReactNode }> = ({ pageName, children }) => {
    const { principal } = useAuth();
    const asks = Boolean(principal);
    const [tenantId, setTenantId] = useState(() => {
        // Named before the page below is first drawn, so that its first
        // request names the tenant.
        if (asks && chosenTenant()) setActingTenant(chosenTenant());
        return asks ? chosenTenant() : '';
    });

    useEffect(() => {
        if (!asks) return undefined;
        setActingTenant(tenantId || null);
        return () => setActingTenant(null);
    }, [asks, tenantId]);

    if (!principal) return <>{children}</>;

    const choose = (next: string) => {
        rememberChosenTenant(next);
        setActingTenant(next || null);
        setTenantId(next);
    };
    const Choice = principal.TenantChoice;

    if (!tenantId) {
        return (
            <div data-testid="tenant-needed" className="flex flex-col items-center justify-center py-20 text-center">
                <div className="w-16 h-16 rounded-full bg-surface-alt flex items-center justify-center mb-4">
                    <Building2 className="w-8 h-8 text-muted" />
                </div>
                <h3 className="text-lg font-semibold text-default mb-2">Choose a tenant</h3>
                <p className="text-muted max-w-md text-sm mb-4">
                    {Choice
                        ? `${pageName} shows the data of one tenant. Choose the tenant to show.`
                        : `${pageName} shows the data of one tenant. Sign in as an administrator of that tenant to see it.`}
                </p>
                {Choice && <Choice value="" onChange={choose} />}
            </div>
        );
    }

    return (
        <div className="app-column h-full">
            <div data-testid="tenant-shown" className="flex-shrink-0 flex items-center gap-3 px-6 py-2 border-b border-default bg-surface-alt text-sm text-secondary">
                <Building2 className="w-4 h-4 text-muted" />
                <span>Showing tenant</span>
                {Choice ? <Choice value={tenantId} onChange={choose} /> : <span className="font-mono">{tenantId}</span>}
            </div>
            <div key={tenantId} className="flex-1 min-h-0">
                {children}
            </div>
        </div>
    );
};
