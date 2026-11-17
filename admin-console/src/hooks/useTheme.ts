/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Theme hook — a re-export of the shared implementation in
 * `brutor-ui-style`, which the portal uses too.
 *
 * At this path so the components that import it do not need to know
 * where the implementation lives.
 */

export {
    applyThemeToDOM,
    ThemeContext,
    THEME_LABELS,
    THEME_NAMES,
    useTheme,
    type FontSizePreset,
    type ThemeContextValue,
    type ThemeName,
    type ThemeSettings,
} from '../ui-style/react/index';

/** `localStorage` key for the operator's theme choice. */
export const STORAGE_KEY = 'admin-theme';

/**
 * Themes the admin console offers.
 *
 * The shared package also ships `high-contrast`, which the portal exposes.
 * Enabling it here is a one-line change — it is left off only because
 * turning it on is a product decision.
 */
// Dark first: it is the default, and a stored theme the console does not
// offer clamps to the first entry.
export const AVAILABLE_THEMES = ['dark', 'light'] as const;
