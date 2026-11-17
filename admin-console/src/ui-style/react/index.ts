/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

export { ThemeProvider, type ThemeProviderProps } from './ThemeProvider';
export { useTheme } from './useTheme';
export { TenantBrandingProvider, TenantBrandingContext, type TenantBrandingProviderProps, type TenantBrandingContextValue } from './TenantBrandingProvider';
export { useTenantBranding } from './useTenantBranding';
export {
    adjustBrightness,
    applyTenantBranding,
    deriveBrandVars,
    hexToRgb,
    updateFavicon,
    type ApplyBrandingOptions,
    type Rgb,
    type TenantBrand,
} from '../theme/branding';
export { ThemeSelector, type ThemeSelectorProps } from './ThemeSelector';
export { FontSizeSelector, type FontSizeSelectorProps } from './FontSizeSelector';
export {
    applyThemeToDOM,
    asFontSize,
    asThemeName,
    DEFAULT_SETTINGS,
    FONT_SIZES,
    FONT_SIZE_LABELS,
    FONT_SIZE_VALUES,
    parseStoredSettings,
    ThemeContext,
    THEME_LABELS,
    THEME_NAMES,
    type FontSizePreset,
    type ThemeContextValue,
    type ThemeName,
    type ThemeSettings,
} from './theme';
