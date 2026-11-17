/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
    applyThemeToDOM,
    asFontSize,
    asThemeName,
    DEFAULT_SETTINGS,
    parseStoredSettings,
    ThemeContext,
    THEME_NAMES,
    type FontSizePreset,
    type ThemeName,
    type ThemeSettings,
} from './theme';

export interface ThemeProviderProps {
    children: React.ReactNode;
    /**
     * `localStorage` key for this app's preferences. Distinct per app so
     * the Admin Console and the portal don't fight over one value when
     * both are open on the same origin in development.
     */
    storageKey: string;
    /**
     * Themes this app offers, in display order. The Admin Console ships
     * light/dark; the portal adds high-contrast. A stored theme outside
     * this list falls back to the first entry, so narrowing the list can
     * never leave a user stuck on a theme the app no longer offers.
     */
    availableThemes?: readonly ThemeName[];
    /** Default when nothing is stored. Must be in `availableThemes`. */
    defaultSettings?: ThemeSettings;
    /** Called after a change so the app can persist to the user's profile. */
    onSettingsChange?: (settings: ThemeSettings) => void;
}

export const ThemeProvider: React.FC<ThemeProviderProps> = ({
    children,
    storageKey,
    availableThemes = THEME_NAMES,
    defaultSettings = DEFAULT_SETTINGS,
    onSettingsChange,
}) => {
    const themes = useMemo(
        () => (availableThemes.length ? availableThemes : THEME_NAMES),
        [availableThemes],
    );

    /** Clamp a theme to what this app actually offers. */
    const clamp = useCallback(
        (settings: ThemeSettings): ThemeSettings =>
            themes.includes(settings.theme)
                ? settings
                : { ...settings, theme: themes[0] },
        [themes],
    );

    const [settings, setSettings] = useState<ThemeSettings>(() => {
        try {
            return clamp(parseStoredSettings(localStorage.getItem(storageKey), defaultSettings));
        } catch {
            // Private mode, blocked site data, or a throwing accessor.
            return { ...defaultSettings };
        }
    });

    /*
     * Held in a ref so `setTheme`/`setFontSize` stay stable for the life of
     * the provider — they are handed to every consumer through context.
     * Assigned in an effect rather than during render: the ref is only read
     * from a `setTimeout` scheduled after commit, so the effect always wins
     * the race, and writing it during render breaks under concurrent
     * rendering (a render that is thrown away must not mutate anything).
     */
    const onSettingsChangeRef = useRef(onSettingsChange);
    useEffect(() => {
        onSettingsChangeRef.current = onSettingsChange;
    }, [onSettingsChange]);

    useEffect(() => {
        applyThemeToDOM(settings);
        try {
            localStorage.setItem(storageKey, JSON.stringify(settings));
        } catch { /* ignore — preferences are a convenience, not state */ }
    }, [settings, storageKey]);

    const update = useCallback((patch: Partial<ThemeSettings>) => {
        setSettings((prev) => {
            const next = clamp({ ...prev, ...patch });
            setTimeout(() => onSettingsChangeRef.current?.(next), 0);
            return next;
        });
    }, [clamp]);

    const setTheme = useCallback((theme: ThemeName) => update({ theme }), [update]);
    const setFontSize = useCallback((fontSize: FontSizePreset) => update({ fontSize }), [update]);

    const applyUserSettings = useCallback(
        (metadata: Record<string, unknown> | undefined) => {
            const stored = metadata?.themeSettings;
            if (typeof stored !== 'object' || stored === null) return;
            const record = stored as Record<string, unknown>;
            const merged = clamp({
                theme: asThemeName(record.theme) ?? defaultSettings.theme,
                fontSize: asFontSize(record.fontSize) ?? defaultSettings.fontSize,
            });
            setSettings(merged);
            applyThemeToDOM(merged);
            try {
                localStorage.setItem(storageKey, JSON.stringify(merged));
            } catch { /* ignore */ }
        },
        [clamp, defaultSettings, storageKey],
    );

    const getSettingsForProfile = useCallback(() => settings, [settings]);

    const value = useMemo(
        () => ({
            theme: settings.theme,
            fontSize: settings.fontSize,
            availableThemes: themes,
            setTheme,
            setFontSize,
            applyUserSettings,
            getSettingsForProfile,
        }),
        [settings, themes, setTheme, setFontSize, applyUserSettings, getSettingsForProfile],
    );

    return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
};
