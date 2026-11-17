/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Per-tenant branding: the colour maths and the DOM application.
 *
 * One implementation, used by the Admin Console and the portal, so the
 * two cannot derive different shades from the same brand colour.
 */

export interface TenantBrand {
    /** Brand colour as `#rrggbb`. Drives the whole `*-primary-*` scale. */
    primary_color?: string | null;
    secondary_color?: string | null;
    favicon_url?: string | null;
    company_name?: string | null;
}

export interface Rgb {
    r: number;
    g: number;
    b: number;
}

/** Parse `#rrggbb` (or `rrggbb`). Returns null for anything else. */
export function hexToRgb(hex: string): Rgb | null {
    const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex.trim());
    return m ? { r: parseInt(m[1], 16), g: parseInt(m[2], 16), b: parseInt(m[3], 16) } : null;
}

/**
 * Move a colour toward white (positive percent) or black (negative).
 * Used to derive the hover/tint steps from the single brand colour a
 * tenant supplies.
 */
export function adjustBrightness(hex: string, percent: number): string {
    const rgb = hexToRgb(hex);
    if (!rgb) return hex;
    const adjust = (v: number) =>
        Math.max(0, Math.min(255, Math.round(v + (255 - v) * (percent / 100))));
    const toHex = (v: number) => adjust(v).toString(16).padStart(2, '0');
    return `#${toHex(rgb.r)}${toHex(rgb.g)}${toHex(rgb.b)}`;
}

/**
 * The CSS variables a brand colour expands into.
 *
 * `--color-primary-ch` (space-separated channels) is what the Tailwind
 * preset reads, so it is what makes a tenant's colour reach all 300-odd
 * `*-primary-*` utilities. Before this existed the portal set only the
 * comma form and hard-coded the brand hex in its Tailwind config, so
 * tenant branding never reached a single utility there.
 *
 * `--color-primary-rgb` (comma form) is kept because existing CSS consumes
 * it as `rgba(var(--color-primary-rgb), a)`.
 */
export function deriveBrandVars(brand: TenantBrand): Record<string, string> {
    const vars: Record<string, string> = {};

    const primary = brand.primary_color?.trim();
    if (primary) {
        vars['--color-primary'] = primary;
        const rgb = hexToRgb(primary);
        if (rgb) {
            vars['--color-primary-rgb'] = `${rgb.r}, ${rgb.g}, ${rgb.b}`;
            vars['--color-primary-ch'] = `${rgb.r} ${rgb.g} ${rgb.b}`;
            vars['--color-primary-dark'] = adjustBrightness(primary, -20);
            vars['--color-primary-light'] = adjustBrightness(primary, 40);
            vars['--color-primary-pale'] = adjustBrightness(primary, 80);
        }
    }

    const secondary = brand.secondary_color?.trim();
    if (secondary) vars['--color-secondary'] = secondary;

    return vars;
}

/** Swap the document favicon. */
export function updateFavicon(href: string): void {
    document.querySelectorAll("link[rel*='icon']").forEach((l) => l.remove());
    const link = document.createElement('link');
    link.rel = 'icon';
    link.href = href;
    document.head.appendChild(link);
}

export interface ApplyBrandingOptions {
    /** Title when the tenant has no company name, e.g. `Brutor Agent Control Admin Console`. */
    defaultTitle: string;
    /** Appended after the company name, e.g. ` - Admin`. */
    titleSuffix?: string;
}

/**
 * Apply branding to the document. Idempotent — safe to call on every
 * light/dark flip and on every re-fetch.
 *
 * Only branding is touched here. Surfaces, text and borders belong to the
 * theme (`data-theme` + the generated token sheet), so a tenant cannot
 * accidentally break contrast by supplying one colour.
 */
export function applyTenantBranding(brand: TenantBrand, options: ApplyBrandingOptions): void {
    const root = document.documentElement;
    for (const [name, value] of Object.entries(deriveBrandVars(brand))) {
        root.style.setProperty(name, value);
    }

    if (brand.favicon_url) updateFavicon(brand.favicon_url);

    const name = brand.company_name?.trim();
    document.title = name ? `${name}${options.titleSuffix ?? ''}` : options.defaultTitle;
}
