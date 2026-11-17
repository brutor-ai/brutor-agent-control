/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

// Split from useTheme.tsx so that file exports only the hook and its types:
// a module exporting both a component and non-components opts out of Fast
// Refresh (react-refresh/only-export-components).
import React from 'react';

import { ThemeProvider as SharedThemeProvider } from '../ui-style/react/index';
import type { ThemeSettings } from '../ui-style/react/index';
import { AVAILABLE_THEMES, STORAGE_KEY } from './useTheme';

/** Dark is the console's default until the operator picks a theme. */
const DEFAULT_THEME_SETTINGS: ThemeSettings = { theme: 'dark', fontSize: 'medium' };

/**
 * Admin console theme provider — the shared provider wired to this app's
 * storage key and theme list.
 */
export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => (
    <SharedThemeProvider
        storageKey={STORAGE_KEY}
        availableThemes={AVAILABLE_THEMES}
        defaultSettings={DEFAULT_THEME_SETTINGS}
    >
        {children}
    </SharedThemeProvider>
);
