/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Theme types, validation and DOM application.
 *
 * Deliberately component-free: a module that exports both components and
 * non-components opts out of React Fast Refresh
 * (`react-refresh/only-export-components`), which is why the provider and
 * the hook live in their own files.
 */

import { createContext } from 'react';

export type ThemeName = 'light' | 'dark' | 'high-contrast';
export type FontSizePreset = 'small' | 'medium' | 'large' | 'xlarge';

export const THEME_NAMES: readonly ThemeName[] = ['light', 'dark', 'high-contrast'] as const;
export const FONT_SIZES: readonly FontSizePreset[] = ['small', 'medium', 'large', 'xlarge'] as const;

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

export const THEME_LABELS: Record<ThemeName, { label: string; description: string }> = {
    light: { label: 'Light', description: 'Clean and bright, ideal for well-lit rooms' },
    dark: { label: 'Dark', description: 'Reduced eye strain, great for extended use' },
    'high-contrast': {
        label: 'High Contrast',
        description: 'Maximum contrast for low vision and bright glare',
    },
};

export interface ThemeSettings {
    theme: ThemeName;
    fontSize: FontSizePreset;
}

export const DEFAULT_SETTINGS: ThemeSettings = { theme: 'light', fontSize: 'medium' };

/* ── Untrusted-input narrowing ──────────────────────────────────────────
 *
 * `localStorage` is user-writable and survives across releases, so its
 * contents are untrusted: `theme` goes straight onto `<html data-theme>`
 * and `fontSize` indexes FONT_SIZE_VALUES. A stale or hand-edited value
 * spread in verbatim would produce an unstyled page and
 * `--font-size-base: undefinedpx`, so anything unrecognised falls back to
 * the default.
 *
 * These are local rather than imported from an app's `lib/narrow.ts` so
 * the package stays self-contained.
 */

export function asThemeName(value: unknown): ThemeName | undefined {
    return THEME_NAMES.find((t) => t === value);
}

export function asFontSize(value: unknown): FontSizePreset | undefined {
    return FONT_SIZES.find((f) => f === value);
}

function readString(source: unknown, key: string): string | undefined {
    if (typeof source !== 'object' || source === null) return undefined;
    const value = (source as Record<string, unknown>)[key];
    return typeof value === 'string' ? value : undefined;
}

/**
 * Read stored settings: the JSON object the provider writes,
 * `{"theme":"dark","fontSize":"small"}`. Anything else, and any field of
 * it that is not a known value, is the fallback.
 */
export function parseStoredSettings(raw: string | null, fallback: ThemeSettings): ThemeSettings {
    if (!raw) return { ...fallback };

    try {
        const parsed: unknown = JSON.parse(raw);
        return {
            theme: asThemeName(readString(parsed, 'theme')) ?? fallback.theme,
            fontSize: asFontSize(readString(parsed, 'fontSize')) ?? fallback.fontSize,
        };
    } catch {
        return { ...fallback };
    }
}

/**
 * Apply settings to the document.
 *
 * `data-theme` selects the theme's variable block from the generated token
 * sheet; `--font-size-base` drives `body` in base.css. Both are set on
 * `<html>` so an inline script in `index.html` can apply the stored theme
 * before first paint and avoid a flash of the wrong theme.
 */
export function applyThemeToDOM(settings: ThemeSettings): void {
    const html = document.documentElement;
    html.setAttribute('data-theme', settings.theme);
    html.style.setProperty('--font-size-base', `${FONT_SIZE_VALUES[settings.fontSize]}px`);
}

export interface ThemeContextValue {
    theme: ThemeName;
    fontSize: FontSizePreset;
    /** Themes this app offers, in display order. */
    availableThemes: readonly ThemeName[];
    setTheme: (theme: ThemeName) => void;
    setFontSize: (size: FontSizePreset) => void;
    /** Apply preferences stored on the user's profile (after login). */
    applyUserSettings: (metadata: Record<string, unknown> | undefined) => void;
    /** Current settings, for persisting back to the profile. */
    getSettingsForProfile: () => ThemeSettings;
}

export const ThemeContext = createContext<ThemeContextValue | null>(null);
