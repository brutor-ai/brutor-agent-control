/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/** Themes shipped by the design system. Consumers opt into a subset. */
export type ThemeName = 'light' | 'dark' | 'high-contrast';

export const THEME_NAMES: readonly ThemeName[] = ['light', 'dark', 'high-contrast'] as const;

/** Font scale presets (portal exposes these; admin currently does not). */
export type FontSizePreset = 'small' | 'medium' | 'large' | 'xlarge';

export const FONT_SIZE_VALUES: Record<FontSizePreset, number> = {
    small: 11,
    medium: 13,
    large: 15,
    xlarge: 17,
};

export const FONT_SIZE_LABELS: Record<FontSizePreset, string> = {
    small: 'Small (11px)',
    medium: 'Medium (13px)',
    large: 'Large (15px)',
    xlarge: 'Extra Large (17px)',
};

/**
 * Which CSS property a colour utility paints. The palette transform is
 * property-scoped because the *same* palette shade needs opposite
 * treatment depending on the property — see `palette.ts`.
 */
export type ColorRole = 'bg' | 'tx' | 'bd';

export const COLOR_ROLES: readonly ColorRole[] = ['bg', 'tx', 'bd'] as const;
