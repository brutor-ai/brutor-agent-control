/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { createContext, useContext, useEffect, useState, ReactNode } from 'react';
import { useAuth } from '../hooks/useAuth';
import { useReadFailures, type ReadFailure } from '../hooks/useRead';
import { ownTenantOf } from '../lib/gatewayApi/tenant';

interface TenantFeaturesContextType {
    features: Record<string, boolean>;
    isFeatureEnabled: (key: string) => boolean;
    displayName: string | null;
    loaded: boolean;
    /** The tenant could not be read: no feature is known to be off, so every feature is shown. */
    failure: ReadFailure | null;
    /** Reads the tenant again. */
    reload: () => void;
}

const TenantFeaturesContext = createContext<TenantFeaturesContextType | undefined>(undefined);

export const TenantFeaturesProvider: React.FC<{ children: ReactNode }> = ({ children }) => {
    // A contributed principal (slot `app.principals`) is of no tenant: there
    // are no tenant features to read for it, and none is off.
    const { isAuthenticated, principal, gatewayClient } = useAuth();
    const [features, setFeatures] = useState<Record<string, boolean>>({});
    const [displayName, setDisplayName] = useState<string | null>(null);
    const [loaded, setLoaded] = useState(false);
    const [readFailure, reads] = useReadFailures<'tenant'>();
    const [asked, setAsked] = useState(0);

    useEffect(() => {
        if (!isAuthenticated || !gatewayClient || principal) {
            setLoaded(true);
            return;
        }
        gatewayClient.getCurrentTenant()
            .then((answer) => {
                const tenant = ownTenantOf(answer);
                setFeatures(tenant?.features ?? {});
                setDisplayName(tenant?.display_name || null);
                reads.loaded('tenant');
            })
            .catch((err: unknown) => {
                reads.failed('tenant', 'Failed to load the features of this tenant; every feature is shown', err);
            })
            .finally(() => setLoaded(true));
    }, [isAuthenticated, gatewayClient, principal, asked, reads]);

    const isFeatureEnabled = (key: string): boolean => {
        if (principal) return true;
        if (Object.keys(features).length === 0) return true;
        return features[key] !== false;
    };

    return (
        <TenantFeaturesContext.Provider
            value={{
                features,
                isFeatureEnabled,
                displayName,
                loaded,
                failure: principal ? null : readFailure('tenant'),
                reload: () => setAsked((n) => n + 1),
            }}
        >
            {children}
        </TenantFeaturesContext.Provider>
    );
};

export const useTenantFeatures = (): TenantFeaturesContextType => {
    const ctx = useContext(TenantFeaturesContext);
    if (!ctx) {
        throw new Error('useTenantFeatures must be used within a TenantFeaturesProvider');
    }
    return ctx;
};
