/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Semantic theme tokens — surfaces, text, borders, brand.
 *
 * Variable NAMES are load-bearing and must not be renamed: both apps set
 * `--color-primary*` and `--color-secondary` on `document.documentElement`
 * at runtime for per-tenant branding (admin `TenantThemeContext`, portal
 * `PortalTenantThemeContext`). Renaming them silently kills tenant themes.
 *
 * The tokens are the union of what the two apps use.
 */

/** Brand defaults. Overwritten per tenant at runtime, so light-mode only. */
const BRAND = {
    '--color-primary': '#1BB5E8',
    '--color-primary-dark': '#0e9fd0',
    '--color-primary-light': '#75d5f3',
    '--color-primary-pale': '#e8f8fd',
    // Two forms of the same channels, both load-bearing:
    //   -rgb  legacy comma form, consumed by `rgba(var(--x), a)` in CSS
    //   -ch   space form, required by `rgb(var(--x) / a)` in the Tailwind
    //         preset so that `/opacity` modifiers work on brand colours
    // Both theme contexts set both at runtime. See README "Brand channels".
    '--color-primary-rgb': '27, 181, 232',
    '--color-primary-ch': '27 181 232',
    '--color-secondary': '#87ceeb',
};

export const SEMANTIC_THEMES = {
    light: {
        ...BRAND,

        '--color-surface': '#ffffff',
        '--color-surface-alt': '#f9fafb',
        '--color-surface-raised': '#f3f4f6',
        '--color-surface-inset': '#e5e7eb',

        '--color-text': '#111827',
        '--color-text-secondary': '#4b5563',
        /*
         * gray-500, not gray-400. Muted text at #9ca3af scored 2.5:1 on
         * `--color-surface-raised` — below even the 3:1 large-text bar —
         * in both apps. Contrast is asserted per surface in
         * tests/contrast.test.mjs.
         */
        '--color-text-muted': '#6b7280',
        '--color-disabled': '#d1d5db',

        '--color-border': '#e5e7eb',
        '--color-border-strong': '#d1d5db',

        '--color-input-bg': '#ffffff',
        '--color-autofill-bg': '#ffffff',
        /*
         * Text/iconography drawn on a saturated accent fill (generated
         * avatar colours, provider initials, brand buttons). White in every
         * theme — the fill is always saturated — but a token rather than a
         * literal so it is themeable if that ever stops being true.
         */
        '--color-on-accent': '#ffffff',
        '--color-shadow': 'rgba(0, 0, 0, 0.1)',

        // Surfaces only the portal uses. Harmless in the Admin Console
        // (nothing references them there) and available if it grows a
        // gradient header or a themed sidebar.
        '--color-header-from': '#1BB5E8',
        '--color-header-to': '#0ea5e9',
        '--color-sidebar-bg': '#ffffff',
        '--color-sidebar-hover': '#f3f4f6',
    },

    dark: {
        // The four surfaces are gray 900/800/700/600.
        '--color-surface': '#111827',
        '--color-surface-alt': '#1f2937',
        '--color-surface-raised': '#374151',
        '--color-surface-inset': '#4b5563',

        // Gray, not slate: the surfaces above are gray, and slate text on
        // them reads very slightly cool-cast. One hue family throughout.
        '--color-text': '#f9fafb',
        '--color-text-secondary': '#d1d5db',
        // gray-400: #6b7280 scored 2.13:1 on `--color-surface-raised`.
        '--color-text-muted': '#9ca3af',
        '--color-disabled': '#4b5563',

        '--color-border': '#374151',
        '--color-border-strong': '#4b5563',

        '--color-input-bg': '#1f2937',
        '--color-autofill-bg': '#1f2937',
        /*
         * Text/iconography drawn on a saturated accent fill (generated
         * avatar colours, provider initials, brand buttons). White in every
         * theme — the fill is always saturated — but a token rather than a
         * literal so it is themeable if that ever stops being true.
         */
        '--color-on-accent': '#ffffff',
        '--color-shadow': 'rgba(0, 0, 0, 0.4)',

        '--color-header-from': '#0c485d',
        '--color-header-to': '#075985',
        '--color-sidebar-bg': '#1f2937',
        '--color-sidebar-hover': '#374151',
    },

    'high-contrast': {
        '--color-surface': '#000000',
        '--color-surface-alt': '#1a1a2e',
        '--color-surface-raised': '#2d2d44',
        '--color-surface-inset': '#3d3d5c',

        '--color-text': '#ffffff',
        '--color-text-secondary': '#e0e0e0',
        '--color-text-muted': '#b0b0b0',
        '--color-disabled': '#707070',

        '--color-border': '#555577',
        '--color-border-strong': '#7777aa',

        '--color-input-bg': '#1a1a2e',
        '--color-autofill-bg': '#1a1a2e',
        /*
         * Text/iconography drawn on a saturated accent fill (generated
         * avatar colours, provider initials, brand buttons). White in every
         * theme — the fill is always saturated — but a token rather than a
         * literal so it is themeable if that ever stops being true.
         */
        '--color-on-accent': '#ffffff',
        '--color-shadow': 'rgba(0, 0, 0, 0.6)',

        '--color-header-from': '#0c485d',
        '--color-header-to': '#075985',
        '--color-sidebar-bg': '#1a1a2e',
        '--color-sidebar-hover': '#2d2d44',
    },
};

/**
 * `color-scheme` per theme, so native form controls, scrollbars and the
 * browser's own chrome follow the app theme rather than the OS setting.
 */
export const COLOR_SCHEME = {
    light: 'light',
    dark: 'dark',
    'high-contrast': 'dark',
};

/**
 * Shadows are tuned per theme: the light-mode ramp is far too weak to read
 * against a dark surface, so dark themes deepen it.
 */
export const SHADOW_OVERRIDES = {
    dark: {
        sm: '0 1px 2px 0 rgba(0, 0, 0, 0.3)',
        md: '0 2px 6px -1px rgba(0, 0, 0, 0.4)',
        lg: '0 4px 12px -2px rgba(0, 0, 0, 0.5)',
        xl: '0 8px 24px -4px rgba(0, 0, 0, 0.6)',
    },
    'high-contrast': {
        sm: '0 1px 2px 0 rgba(0, 0, 0, 0.3)',
        md: '0 2px 6px -1px rgba(0, 0, 0, 0.4)',
        lg: '0 4px 12px -2px rgba(0, 0, 0, 0.5)',
        xl: '0 8px 24px -4px rgba(0, 0, 0, 0.6)',
    },
};

/** Default base font size per font-scale preset (portal exposes a picker). */
export const FONT_SIZE_VALUES = { small: 11, medium: 13, large: 15, xlarge: 17 };
