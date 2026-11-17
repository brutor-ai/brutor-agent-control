/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { createContext, useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { applyTenantBranding, type TenantBrand } from '../theme/branding';
import type { ThemeName } from './theme';
import { useTheme } from './useTheme';

export interface TenantBrandingContextValue {
    brand: TenantBrand | null;
    isLoading: boolean;
    /** Re-fetch and re-apply (after an operator edits the tenant theme). */
    reload: () => void;
}

export const TenantBrandingContext = createContext<TenantBrandingContextValue | null>(null);

export interface TenantBrandingProviderProps {
    children: React.ReactNode;
    /**
     * Fetch the brand for the active light/dark mode. App-specific: the
     * Admin Console reads tenant theme variants from the Control Plane,
     * the User Portal reads them from the Core Gateway — so the transport stays
     * in the app and only the application of the result is shared.
     *
     * Return `null` to leave the default Brutor branding in place.
     */
    load: (mode: ThemeName) => Promise<TenantBrand | null>;
    /** Document title when the tenant has no company name. */
    defaultTitle: string;
    /** Appended after a company name, e.g. `' - Admin'`. */
    titleSuffix?: string;
    /**
     * `localStorage` key for the last-known brand. When set, the cached
     * brand is applied synchronously on mount so a tenant's colour is
     * already correct at first paint instead of flashing Brutor cyan
     * while the fetch is in flight.
     */
    cacheKey?: string;
}

function readCache(key: string | undefined): TenantBrand | null {
    if (!key) return null;
    try {
        const raw = localStorage.getItem(key);
        if (!raw) return null;
        const parsed: unknown = JSON.parse(raw);
        // Untrusted: only carry through the fields we apply, as strings.
        if (typeof parsed !== 'object' || parsed === null) return null;
        const rec = parsed as Record<string, unknown>;
        const pick = (k: string) => (typeof rec[k] === 'string' ? (rec[k] as string) : undefined);
        return {
            primary_color: pick('primary_color'),
            secondary_color: pick('secondary_color'),
            favicon_url: pick('favicon_url'),
            company_name: pick('company_name'),
        };
    } catch {
        return null;
    }
}

/**
 * Applies per-tenant branding, and re-applies it when the user flips
 * light/dark (tenants can supply a different brand colour per mode).
 */
export const TenantBrandingProvider: React.FC<TenantBrandingProviderProps> = ({
    children,
    load,
    defaultTitle,
    titleSuffix,
    cacheKey,
}) => {
    const { theme: mode } = useTheme();

    const cached = useRef<TenantBrand | null>(null);
    if (cached.current === null) cached.current = readCache(cacheKey);

    const [brand, setBrand] = useState<TenantBrand | null>(cached.current);
    const [isLoading, setIsLoading] = useState(true);
    const [reloadToken, setReloadToken] = useState(0);

    // First paint: apply whatever was cached before the fetch resolves.
    const appliedCache = useRef(false);
    if (!appliedCache.current && cached.current && typeof document !== 'undefined') {
        appliedCache.current = true;
        applyTenantBranding(cached.current, { defaultTitle, titleSuffix });
    }

    // `load` is frequently an inline arrow in the consumer; hold it in a
    // ref so a new identity does not re-trigger the fetch on every render.
    const loadRef = useRef(load);
    useEffect(() => {
        loadRef.current = load;
    }, [load]);

    useEffect(() => {
        let cancelled = false;
        setIsLoading(true);

        loadRef.current(mode)
            .then((next) => {
                if (cancelled) return;
                const effective = next ?? cached.current;
                if (effective) {
                    applyTenantBranding(effective, { defaultTitle, titleSuffix });
                    setBrand(effective);
                    if (cacheKey && next) {
                        try {
                            localStorage.setItem(cacheKey, JSON.stringify(next));
                        } catch { /* ignore — a cache miss only costs a flash */ }
                    }
                } else {
                    // No tenant branding: make sure the title is still right.
                    applyTenantBranding({}, { defaultTitle, titleSuffix });
                }
            })
            .catch(() => {
                // Branding is cosmetic; a failed fetch must never block the
                // app. The cached or default brand stays in place.
            })
            .finally(() => {
                if (!cancelled) setIsLoading(false);
            });

        return () => {
            cancelled = true;
        };
    }, [mode, reloadToken, defaultTitle, titleSuffix, cacheKey]);

    const reload = useCallback(() => setReloadToken((n) => n + 1), []);

    const value = useMemo(() => ({ brand, isLoading, reload }), [brand, isLoading, reload]);

    return (
        <TenantBrandingContext.Provider value={value}>{children}</TenantBrandingContext.Provider>
    );
};
