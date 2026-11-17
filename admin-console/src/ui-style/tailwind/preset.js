/*
 * Copyright 2026 Brutor AI Ltd.
 * SPDX-License-Identifier: Apache-2.0
 */

/**
 * Shared Tailwind preset for the Admin Console and the User Portal.
 *
 * Consume it from the app's own `tailwind.config.js`:
 *
 *     import brutorPreset from './src/ui-style/tailwind/preset.js';
 *     export default { presets: [brutorPreset], content: [...] };
 *
 * The app keeps `content` (paths are app-relative) and may `extend`
 * anything app-specific. Everything colour- or theme-related belongs here,
 * so the two apps cannot drift apart.
 */

import { SHADES, FAMILIES } from '../theme/palette.mjs';

/**
 * Tailwind hands a colour function `var(--tw-bg-opacity)` (or the text /
 * border equivalent) for a *bare* utility, so that the legacy
 * `bg-opacity-*` utilities keep working. That is not an author-specified
 * opacity, and it must not be mistaken for one: treating it as one makes
 * `bg-blue-50` resolve to `rgb(<blue-500> / 1)` in dark mode — a solid
 * mid-blue chip instead of a 10% tint. Pinned by tests/tailwind-build.test.mjs.
 */
const isTailwindOpacityVar = (value) =>
    typeof value === 'string' && value.includes('--tw-');

/**
 * A brand colour that reads its channels from a CSS variable.
 *
 * `defaultAlpha` is the theme's intent and wins over Tailwind's opacity
 * variable; an explicit modifier (`bg-primary-50/50`) still overrides it.
 */
const varColor = (channels, defaultAlpha = 1) => ({ opacityValue }) => {
    const alpha =
        opacityValue === undefined || isTailwindOpacityVar(opacityValue)
            ? defaultAlpha
            : opacityValue;
    return `rgb(${channels} / ${alpha})`;
};

/**
 * One palette family/shade for one role (`bg` | `tx` | `bd`).
 *
 * The fallback chain is the reason the generated CSS stays small and the
 * reason author-written `dark:` variants keep working:
 *
 *   `var(--bg-blue-50, var(--p-blue-50))`
 *
 * A theme declares `--bg-blue-50` only when it wants to change that exact
 * colour for that exact role. Otherwise it falls through to `--p-blue-50`,
 * Tailwind's published value. So `dark:bg-red-900/20` still paints a dark
 * red in dark mode (no theme remaps background shade 900), while
 * `text-red-900` lightens (the dark theme does remap text shade 900).
 *
 * Alpha resolves through the same kind of chain, which is what lets all
 * three of these coexist:
 *
 *   `bg-blue-50`       theme tint if the theme sets one, else opaque
 *   `bg-blue-50/40`    author's 40% always wins
 *   `bg-opacity-50`    still works where no theme tint applies
 */
const roleColor = (role, family, shade) => ({ opacityValue }) => {
    const channels = `var(--${role}-${family}-${shade}, var(--p-${family}-${shade}))`;

    // An explicit modifier is the author speaking; nothing overrides it.
    if (opacityValue !== undefined && !isTailwindOpacityVar(opacityValue)) {
        return `rgb(${channels} / ${opacityValue})`;
    }

    // Otherwise: the theme's alpha for this colour, falling back to
    // Tailwind's opacity variable (and then to fully opaque).
    const themeAlpha = `--${role}-${family}-${shade}-a`;
    return `rgb(${channels} / var(${themeAlpha}, ${opacityValue ?? 1}))`;
};

/** The full palette for one role, as a Tailwind colour object. */
function paletteFor(role) {
    const out = {};
    for (const family of FAMILIES) {
        out[family] = {};
        for (const shade of SHADES) {
            out[family][shade] = roleColor(role, family, shade);
        }
    }
    return out;
}

/**
 * Brand scale. Driven entirely by `--color-primary*`, which both apps
 * rewrite per tenant at runtime — so a tenant's colour reaches every
 * `*-primary-*` utility instead of only the handful of places that
 * happened to use `var(--color-primary)` directly.
 *
 * Shades 50–400 are alpha steps of the brand hue (there is only one brand
 * colour, not a hand-built ramp); 500 is the colour itself; 600+ are the
 * darker brand tone.
 */
const primary = {
    50: varColor('var(--color-primary-ch)', 0.1),
    100: varColor('var(--color-primary-ch)', 0.2),
    200: varColor('var(--color-primary-ch)', 0.3),
    300: varColor('var(--color-primary-ch)', 0.5),
    400: varColor('var(--color-primary-ch)', 0.7),
    DEFAULT: 'var(--color-primary)',
    500: 'var(--color-primary)',
    600: 'var(--color-primary-dark)',
    700: 'var(--color-primary-dark)',
    800: 'var(--color-primary-dark)',
    900: 'var(--color-primary-dark)',
    light: 'var(--color-primary-light)',
    pale: 'var(--color-primary-pale)',
};

/** Semantic surfaces/inputs/sidebar, shared by every role. */
const semanticColors = {
    primary,
    secondary: 'var(--color-secondary)',
    surface: {
        DEFAULT: 'var(--color-surface)',
        alt: 'var(--color-surface-alt)',
        raised: 'var(--color-surface-raised)',
        inset: 'var(--color-surface-inset)',
    },
    input: 'var(--color-input-bg)',
    sidebar: {
        DEFAULT: 'var(--color-sidebar-bg)',
        hover: 'var(--color-sidebar-hover)',
    },
    // An alias of the brand colour; markup uses it as `border-sky-border`.
    'sky-border': 'var(--color-primary)',
};

/** @type {import('tailwindcss').Config} */
export default {
    // The apps drive the theme with `data-theme` on <html> (ThemeProvider),
    // not the OS `prefers-color-scheme`. Tailwind's default is `media`,
    // under which `dark:` utilities fire off the operating system setting
    // instead of the in-app toggle.
    darkMode: ['class', '[data-theme="dark"]'],

    theme: {
        extend: {
            // `colors` stays the literal palette. It feeds the scales we do
            // not theme (gradients, fill, stroke, shadow, accent, caret,
            // outline, decoration) plus the semantic tokens.
            colors: semanticColors,

            // Role-scoped, theme-aware palettes. See `roleColor`.
            backgroundColor: { ...paletteFor('bg'), ...semanticColors },
            textColor: {
                ...paletteFor('tx'),
                ...semanticColors,
                default: 'var(--color-text)',
                secondary: 'var(--color-text-secondary)',
                muted: 'var(--color-text-muted)',
                disabled: 'var(--color-disabled)',
            },
            borderColor: {
                ...paletteFor('bd'),
                ...semanticColors,
                default: 'var(--color-border)',
                strong: 'var(--color-border-strong)',
            },
            divideColor: {
                ...paletteFor('bd'),
                default: 'var(--color-border)',
                strong: 'var(--color-border-strong)',
            },
            ringColor: { ...paletteFor('bd'), ...semanticColors },
            placeholderColor: {
                ...paletteFor('tx'),
                muted: 'var(--color-text-muted)',
            },

            boxShadowColor: { theme: 'var(--color-shadow)' },

            animation: { float: 'float 3s ease-in-out infinite' },
            keyframes: {
                float: {
                    '0%, 100%': { transform: 'translateY(0px) rotate(0deg)' },
                    '33%': { transform: 'translateY(-1px) rotate(1deg)' },
                    '66%': { transform: 'translateY(1px) rotate(-1deg)' },
                },
            },
        },
    },

    plugins: [
        /**
         * `dark-any:` — fires for every dark-ish theme, not just `dark`.
         *
         * `darkMode` above can only name one selector on Tailwind 3.3
         * (`darkMode: 'variant'` with a selector list landed in 3.4), so
         * `dark:` covers `data-theme="dark"` only. Hand-written exceptions
         * that should also apply to high-contrast use `dark-any:`.
         */
        function brutorVariants({ addVariant }) {
            addVariant('dark-any', [
                '[data-theme="dark"] &',
                '[data-theme="high-contrast"] &',
            ]);
            addVariant('high-contrast', '[data-theme="high-contrast"] &');
        },
    ],
};
