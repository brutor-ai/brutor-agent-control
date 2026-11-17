/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { createContext, useContext, useEffect, useState, ReactNode } from 'react';
import { useGateway } from '../hooks/useGateway';
import { useAuth } from '../hooks/useAuth';
import { useTheme } from '../hooks/useTheme';
import { applyTenantBranding } from '../ui-style/react/index';
import { runBackground } from '../lib/asyncSafe';
import { consoleName, PRODUCT_NAME } from '../app/productName';
import { errorKind } from '../lib/narrow';

/**
 * Tenant Theme Configuration
 * This defines the complete branding for a tenant
 */
export interface TenantTheme {
    // Primary brand color (used for headers, buttons, links)
    primary_color: string;
    // Secondary brand color (used for accents, highlights)
    secondary_color: string;
    // Background colors
    background_color: string;
    surface_color: string;
    // Text colors
    text_primary: string;
    text_secondary: string;
    // Border colors
    border_color: string;
    // Logo URL (optional)
    logo_url?: string;
    // Company name override (optional)
    company_name?: string;
    // Favicon URL (optional)
    favicon_url?: string;
}

// ===== 4-variant theme types =====
// Each surface (admin / portal) × mode (light / dark) gets its own
// palette + logo + favicon. `company_name` is shared and lives on
// the bundle wrapper.

export type ThemeVariantKey =
    | 'admin_light'
    | 'admin_dark'
    | 'portal_light'
    | 'portal_dark';

export const THEME_VARIANT_KEYS: ThemeVariantKey[] = [
    'admin_light',
    'admin_dark',
    'portal_light',
    'portal_dark',
];

// One variant's fields. All optional on the wire; the backend merges
// in defaults so what comes back is always fully populated.
export interface TenantThemeVariantFields {
    primary_color?: string;
    secondary_color?: string;
    background_color?: string;
    surface_color?: string;
    text_primary?: string;
    text_secondary?: string;
    border_color?: string;
    logo_url?: string | null;
    favicon_url?: string | null;
}

// Shape of `/v1/admin/tenants/current/theme-variants` (and `/{id}/theme-variants`) and the public
// portal endpoint. The server fills what a variant does not override
// with its defaults, so each variant is fully populated.
export interface TenantThemeVariantsBundle {
    tenant_id: string;
    company_name?: string | null;
    variants: Record<ThemeVariantKey, TenantThemeVariantFields>;
}

// PUT payload — all variants optional; sending {} for a variant
// clears its overrides (server falls back to defaults).
export interface TenantThemeVariantsUpdate {
    company_name?: string | null;
    admin_light?: TenantThemeVariantFields;
    admin_dark?: TenantThemeVariantFields;
    portal_light?: TenantThemeVariantFields;
    portal_dark?: TenantThemeVariantFields;
}

/**
 * Default theme (sky blue), named after the product
 */
const DEFAULT_THEME: TenantTheme = {
    primary_color: '#1BB5E8',
    secondary_color: '#87ceeb',
    background_color: '#ffffff',
    surface_color: '#f8f9fa',
    text_primary: '#000000',
    text_secondary: '#666666',
    border_color: '#e0e0e0',
    company_name: PRODUCT_NAME
};

interface TenantThemeContextType {
    theme: TenantTheme;
    setTheme: (theme: TenantTheme) => void;
    refreshTheme: () => Promise<void>;
    resetTheme: () => void;
    isDefaultTheme: boolean;
    isLoading: boolean;
}

const TenantThemeContext = createContext<TenantThemeContextType | undefined>(undefined);

/**
 * Hook to use tenant theme context
 */
export const useTenantTheme = () => {
    const context = useContext(TenantThemeContext);
    if (!context) {
        throw new Error('useTenantTheme must be used within TenantThemeProvider');
    }
    return context;
};

interface TenantThemeProviderProps {
    children: ReactNode;
}

/**
 * Provider component that manages tenant theming
 * Automatically loads theme from backend when user logs in
 */
export const TenantThemeProvider: React.FC<TenantThemeProviderProps> = ({ children }) => {
    const { fetchCurrentTenantThemeVariants } = useGateway();
    const { user, isAuthenticated, getCurrentTenantId } = useAuth();
    // The admin Console's light/dark switch is owned by `useTheme`
    // (writes `data-theme="dark"` on <html> + localStorage). Watch
    // it so when the operator flips modes we re-apply the matching
    // variant (admin_light vs admin_dark) without re-fetching.
    const { theme: activeMode } = useTheme();
    /*
     * Tenant theme bundles carry a light and a dark variant. `theme` can
     * also be `high-contrast` (the shared provider ships it and the
     * portal exposes it), so anything that is not light selects the dark
     * variant — the same mapping the portal uses.
     */
    const mode: 'light' | 'dark' = activeMode === 'light' ? 'light' : 'dark';

    const [bundle, setBundle] = useState<TenantThemeVariantsBundle | null>(null);
    const [theme, setThemeState] = useState<TenantTheme>(DEFAULT_THEME);
    const [isLoading, setIsLoading] = useState(true);

    /**
     * Apply theme to CSS custom properties
     */
    const applyTheme = (newTheme: TenantTheme) => {
        /*
         * The colour maths, the CSS variables (including the
         * space-separated channel form the Tailwind preset reads), the
         * favicon swap and the document title all live in the shared
         * package, so the portal applies branding identically.
         *
         * Only branding is touched: surfaces, text and borders belong to
         * the theme, so a tenant cannot break contrast with one colour.
         */
        applyTenantBranding(
            {
                primary_color: newTheme.primary_color,
                secondary_color: newTheme.secondary_color,
                favicon_url: newTheme.favicon_url,
                // The title of the tab: a tenant's own company name, or the
                // name of the console. The default theme's company name is
                // the console's wording for a tenant that has set none, not
                // a tenant's, and is not put in the title.
                company_name: newTheme.company_name === DEFAULT_THEME.company_name ? undefined : newTheme.company_name,
            },
            { defaultTitle: consoleName(), titleSuffix: ' - Admin' },
        );
    };

    /**
     * Resolve the active variant (admin_light vs admin_dark) from a
     * fetched bundle, merging in the shared company_name. Returns a
     * fully-populated TenantTheme ready to feed into `applyTheme`.
     */
    const resolveActiveTheme = (
        b: TenantThemeVariantsBundle | null,
        currentMode: 'light' | 'dark',
    ): TenantTheme => {
        if (!b) return DEFAULT_THEME;
        const key: ThemeVariantKey = currentMode === 'dark' ? 'admin_dark' : 'admin_light';
        const variant = b.variants[key] || {};
        return {
            primary_color: variant.primary_color || DEFAULT_THEME.primary_color,
            secondary_color: variant.secondary_color || DEFAULT_THEME.secondary_color,
            background_color: variant.background_color || DEFAULT_THEME.background_color,
            surface_color: variant.surface_color || DEFAULT_THEME.surface_color,
            text_primary: variant.text_primary || DEFAULT_THEME.text_primary,
            text_secondary: variant.text_secondary || DEFAULT_THEME.text_secondary,
            border_color: variant.border_color || DEFAULT_THEME.border_color,
            logo_url: variant.logo_url || undefined,
            favicon_url: variant.favicon_url || undefined,
            company_name: b.company_name || DEFAULT_THEME.company_name,
        };
    };

    /**
     * Load the 4-variant bundle from the backend with localStorage
     * cache for instant first paint. The bundle holds all four
     * variants; we apply the one matching the current useTheme mode
     * and re-apply on mode flips via the effect below.
     */
    const loadTheme = async (tenantId: string) => {
        try {
            const cacheKey = `tenant-theme-bundle-${tenantId}`;
            const cached = localStorage.getItem(cacheKey);
            if (cached) {
                try {
                    const parsed: TenantThemeVariantsBundle = JSON.parse(cached);
                    setBundle(parsed);
                    const resolved = resolveActiveTheme(parsed, mode);
                    setThemeState(resolved);
                    applyTheme(resolved);
                } catch (error) {
                    console.warn('Failed to parse cached theme bundle:', errorKind(error));
                }
            }

            // The caller's own tenant (`/v1/admin/tenants/current/theme-variants`);
            // `tenantId` only keys the local cache.
            const fresh = await fetchCurrentTenantThemeVariants();
            setBundle(fresh);
            const resolved = resolveActiveTheme(fresh, mode);
            setThemeState(resolved);
            applyTheme(resolved);
            localStorage.setItem(cacheKey, JSON.stringify(fresh));
        } catch (error) {
            console.error('Failed to load tenant theme bundle, using default:', errorKind(error));
            setBundle(null);
            setThemeState(DEFAULT_THEME);
            applyTheme(DEFAULT_THEME);
        } finally {
            setIsLoading(false);
        }
    };

    // Re-apply the matching variant whenever the operator toggles
    // light/dark. We don't refetch — the bundle is already in memory.
    useEffect(() => {
        if (!bundle) return;
        const resolved = resolveActiveTheme(bundle, mode);
        setThemeState(resolved);
        applyTheme(resolved);
    }, [mode, bundle]);

    /**
     * Set new theme and apply it
     */
    const setTheme = (newTheme: TenantTheme) => {
        setThemeState(newTheme);
        applyTheme(newTheme);

        // Update cache
        const tenantId = getCurrentTenantId?.();
        if (tenantId) {
            try {
                localStorage.setItem(`tenant-theme-${tenantId}`, JSON.stringify(newTheme));
            } catch (error) {
                console.warn('Failed to cache theme:', errorKind(error));
            }
        }
    };

    /**
     * Refresh theme from backend
     */
    const refreshTheme = async () => {
        const tenantId = getCurrentTenantId?.();
        if (tenantId) {
            setIsLoading(true);
            await loadTheme(tenantId);
        }
    };

    /**
     * Reset to default theme
     */
    const resetTheme = () => {
        setThemeState(DEFAULT_THEME);
        applyTheme(DEFAULT_THEME);

        // Clear cache
        const tenantId = getCurrentTenantId?.();
        if (tenantId) {
            try {
                localStorage.removeItem(`tenant-theme-${tenantId}`);
            } catch (error) {
                console.warn('Failed to clear theme cache:', errorKind(error));
            }
        }
    };

    /**
     * Check if current theme is the default
     */
    const isDefaultTheme = JSON.stringify(theme) === JSON.stringify(DEFAULT_THEME);

    // Load theme when user authenticates or tenant changes
    useEffect(() => {
        const initTheme = async () => {
            if (!isAuthenticated) {
                setThemeState(DEFAULT_THEME);
                applyTheme(DEFAULT_THEME);
                setIsLoading(false);
                return;
            }

            const tenantId = getCurrentTenantId?.();

            if (!tenantId) {
                setThemeState(DEFAULT_THEME);
                applyTheme(DEFAULT_THEME);
                setIsLoading(false);
                return;
            }

            // Load theme for this tenant
            await loadTheme(tenantId);
        };

        runBackground(initTheme(), 'init theme');
    }, [isAuthenticated, user?.tenant_id]); // Watch for tenant_id changes

    return (
        <TenantThemeContext.Provider value={{
            theme,
            setTheme,
            refreshTheme,
            resetTheme,
            isDefaultTheme,
            isLoading
        }}>
            {children}
        </TenantThemeContext.Provider>
    );
};
